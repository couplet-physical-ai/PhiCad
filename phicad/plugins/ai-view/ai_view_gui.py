#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, you may find one here:
# http://www.gnu.org/licenses/gpl-2.0.html
#
"""The AI View window: live board context + chat, wired into the running PhiCad.

Launched by the PCB editor's toolbar button (through ai_view.sh, runtime type "exec").
KiCad injects KICAD_API_SOCKET / KICAD_API_TOKEN into this process and sets cwd to the
plugin directory, so board_context.connect() just works.  It also runs standalone
against `phicad api-server` for fast iteration.

TWO MODES
  Manual  the original split: status block, the notebook of views on the left, chat on
          the right of a draggable sash.  The user works in the real PCB editor and this
          window is a reference pane; "Focus Editor" puts PhiCad back in front.
  Agent   the rendered view fills a maximised window with no tab strip and no status
          block, and the chat becomes a small panel floating over it that opens and
          closes without leaving the mode.  Chat drives that view with /view commands.

  Nothing is created or destroyed on a switch -- the SAME widgets are re-managed -- so
  an in-flight render, the transcript and the phi session all survive it.  That is not
  a nicety: Tk cannot reparent a widget, so a mode switch that rebuilt the chat would
  have to throw away its state, and re-adding it to the PanedWindow afterwards fails
  silently (an unmapped tab, no exception).  The chat frame is therefore a child of the
  PanedWindow forever, and `place(in_=self.left)` -- legal because `left` is a
  descendant of the chat's parent -- is what makes the floating panel possible at all.

  Agent mode uses `root.state("zoomed")`, not `wm attributes -fullscreen`: measured on
  Tk 9.0/aqua, fullscreen strips the title bar (so the window loses its close button)
  and does not restore the previous geometry when cleared, while "zoomed" fills the
  display work area, keeps the chrome, stays in the current Space -- so the editor can
  still be raised over it -- and `state("normal")` restores the old geometry exactly.
  PHICAD_AI_FULLSCREEN=1 opts into the chrome-free version anyway.

LOOK
  Light shell, ink canvas -- the same shape as PhiCad itself, and a measured choice
  rather than a taste: on Tk 9.0/aqua every ttk background is silently ignored (the
  probe and its results are in ui_theme.py), so a dark window would mean rebuilding
  every control out of tk.Label.  Colour therefore lives where it is honoured and
  where it carries meaning: the render canvas, code blocks, and the role of each
  message.  All of it comes from phicad-altium.json's layer colours.

THE TRANSCRIPT
  A model answer is not prose.  It arrives as markdown with fenced blocks, and the
  design IR arrives as sixty lines of JSON the user is expected to READ before
  running /design save.  So the transcript is a formatter, not a log: fenced blocks
  become monospace cards on the board's own background with JSON syntax colouring,
  bold and `code` are honoured inline, and every block says what it is.

THREADING MODEL
  tkinter is single-threaded: every IPC read and every AI call runs on a worker
  thread and posts a closure to `self._events` (queue.Queue); the Tk main loop
  drains that queue every 60 ms.  Workers never touch widgets directly.

DEGRADATION (never fabricate)
  * kipy missing / socket dead  -> context pane shows the failure + how to fix it.
  * no board open               -> status + context pane say so; chat still works.
  * no `phi` backend            -> chat input stays live, but "send" prints the exact
                                   context+message that WOULD have been sent, clearly
                                   labelled; no fake AI answer, ever.
  * no editor process           -> "Focus Editor" and /editor raise refuse and say why,
                                   rather than launching a second PhiCad.
  * unwritable config home      -> the mode still switches; the transcript says once
                                   that it will not be remembered, and where it tried.

TEST HOOKS
  PHICAD_AI_AUTOSEND="question"  auto-sends one message once the first context refresh
                                 lands (used by the headless end-to-end verification).
  PHICAD_AI_TAB=pcb              open straight to a tab.
  PHICAD_AI_MODE=agent|manual    override the remembered mode for one launch.
  PHICAD_AI_PROBE="/mode agent;/chat close"
                                 run those chat lines on a timer and dump a JSON
                                 widget-geometry snapshot after each one, so the layout
                                 can be asserted on without a human clicking.
  PHICAD_AI_PROBE_MS / _OUT / _HOLD   step delay, output file, keep the window open.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time

import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

# Make sibling modules importable no matter how we were launched (symlinked plugin
# dir, direct `python ai_view_gui.py`, or cwd somewhere else entirely).
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import board_context                                             # noqa: E402
import board_edits                                               # noqa: E402
import board_views                                               # noqa: E402
import bom_view                                                  # noqa: E402
import chat_backend                                              # noqa: E402
import design_commands                                           # noqa: E402
import editor_control                                            # noqa: E402
import host_bridge                                               # noqa: E402
import project_history                                           # noqa: E402
import ui_state                                                  # noqa: E402
import ui_theme                                                  # noqa: E402
import view_commands                                             # noqa: E402

POLL_MS = 60
CONNECT_TIMEOUT_MS = 20000

# How often a "working" line redraws.  Fast enough that the elapsed seconds tick
# visibly -- which is the whole point of it -- and slow enough that a 40 s build is
# not four hundred redraws.
TICK_MS = 250
SPINNER = "|/-\\"

# Every command the entry box can complete, assembled from the three command modules so
# that a command cannot exist in one of them and be missing from the picker.
ALL_COMMANDS = (board_edits.COMMANDS + view_commands.COMMANDS
                + design_commands.COMMANDS)


# Each image tab's toolbar: what to render and how.  Kept as data so adding a view (inner
# layers, bottom silkscreen, an exploded 3D angle) needs no new widget code.
VIEW_SPECS = {
    "pcb": {
        # Front first and default.  "Both" paints the back copper under the front, and
        # with the ground pour filled the back is a solid sheet: legible now that F.Cu is
        # painted last, but still a blue field with the front laid over it.  The view that
        # answers "what is on this board" is the front, so that is what opens.
        "modes": [("Front", board_views.LAYERS_FRONT),
                  ("Back", board_views.LAYERS_BACK),
                  ("Both", board_views.LAYERS_BOTH)],
        "default": 0,
    },
    "3d": {
        "modes": [("Top", "top"), ("Bottom", "bottom"),
                  ("Front", "front"), ("Left", "left")],
        "default": 0,
    },
    "sch": {"modes": [], "default": 0},
}


class ImageTab:
    """One rendered view: a zoomable/pannable image plus its own small toolbar.

    Rendering happens on a worker thread and the result is handed back through the app's
    event queue, because kicad-cli takes ~1 s for a 2D plot and ~3 s for a 3D render and
    blocking the Tk thread for that long makes the whole window look hung.

    Zoom is implemented by resampling the ORIGINAL PIL image rather than scaling the Tk
    photo, so zooming in stays sharp instead of turning into mush.
    """

    def __init__(self, notebook: ttk.Notebook, key: str, app: "AiViewApp"):
        self.key = key
        self.app = app
        self.spec = VIEW_SPECS[key]
        self.rendered = False          # has a render been attempted for the current board?
        self.busy = False
        self.pil_image = None          # the full-resolution render
        self.photo = None              # the Tk image currently on the canvas (keep a ref!)
        self.zoom = 1.0
        self._ImageTk = None           # set once Pillow's ImageTk is known to import
        self.fit_zoom = 1.0            # zoom that fits the viewport; "Fit" returns here
        self._drag = None

        fonts = app.fonts
        self.frame = ttk.Frame(notebook, padding=(0, 6, 0, 0))

        bar = ttk.Frame(self.frame, padding=(10, 0, 10, 0))
        bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        # Controls in their OWN frames, packed as units.  pack shrinks whichever widget
        # runs out of cavity, and with eight siblings competing directly that was
        # "Re-render" -- clipped to a one-letter button that still worked and looked like
        # a rendering artefact.  Two groups plus a label between them cannot do that: the
        # label is the only thing that expands, so it is the only thing that gets cut.
        modes = ttk.Frame(bar)
        modes.pack(side=tk.LEFT)
        self.mode = tk.IntVar(value=self.spec["default"])
        for i, (name, _) in enumerate(self.spec["modes"]):
            ttk.Radiobutton(modes, text=name, value=i, variable=self.mode,
                            style="Toolbutton",
                            command=self.render).pack(side=tk.LEFT, padx=(0, 2))

        self.controls = ttk.Frame(bar)
        self.controls.pack(side=tk.RIGHT)
        # Glyphs rather than words, and the tightest widths ttk will accept: the 3D tab
        # has four mode buttons, and with "Re-render" spelled out that row wanted about
        # 600 px against the 594 the manual split gives it -- which clipped "Fit" to
        # "Fi".  Measured in layout_snapshot as reqw <= w, so the margin is asserted
        # rather than eyeballed.
        self.reload_btn = ttk.Button(self.controls, text="↻", width=2,
                                     style="Small.TButton", command=self.render)
        self.reload_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(self.controls, text="−", width=2, style="Small.TButton",
                   command=lambda: self.zoom_by(1 / 1.25)).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(self.controls, text="+", width=2, style="Small.TButton",
                   command=lambda: self.zoom_by(1.25)).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(self.controls, text="Fit", width=3, style="Small.TButton",
                   command=self.fit).pack(side=tk.LEFT)

        # Spacer, so the two groups sit at the ends of the bar.  The size/time readout is
        # NOT here: measured at the manual split's pane width the row has 44 px left over
        # after the controls, which clipped "1600x2416 · 0.6s" to "16" -- worse than
        # absent.  It is drawn on the canvas instead (see _set_caption), where it costs
        # the layout nothing and reads as a viewer caption rather than a control.
        ttk.Frame(bar).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # The theme's own board background, not a generic dark grey: a plot has no page
        # fill, so the canvas around it continues the board's void instead of framing it
        # in a colour the board never uses.  It also composites the transparent-backed
        # 3D render the way the editor's 3D viewer does.
        self.canvas = tk.Canvas(self.frame, background=ui_theme.CANVAS,
                                highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        # A chip behind the caption, because the caption sits on the picture and the
        # picture is not always dark: on the schematic's cream sheet dim grey text was
        # illegible and on top of the symbols.
        self.caption_bg = self.canvas.create_rectangle(0, 0, 0, 0, outline="",
                                                       fill=ui_theme.INK, state=tk.HIDDEN)
        self.caption = self.canvas.create_text(12, 0, text="", anchor="sw",
                                               fill=ui_theme.CODE_DIM,
                                               font=fonts.mono_small)

        # wraplength must track the pane, not be a constant: a fixed 520 px clipped the
        # "no schematic" explanation mid-word when the paned divider sat left of that.
        # It is updated from <Configure> below.
        self.message = ttk.Label(self.frame, text="", font=fonts.ui,
                                 foreground=ui_theme.TEXT_DIM,
                                 wraplength=480, justify="left", anchor="nw")

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.canvas.bind("<Double-Button-1>", lambda e: self.fit())
        # Trackpad pinch on macOS arrives as MouseWheel with the Command modifier; plain
        # wheel scrolls. Both zoom here because the canvas has nothing else to scroll.
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Configure>", self._on_resize)
        self.frame.bind("<Configure>", self._on_frame_resize)

    # ---------------------------------------------------------------- rendering

    def _selected(self):
        modes = self.spec["modes"]
        if not modes:
            return None
        return modes[max(0, min(self.mode.get(), len(modes) - 1))][1]

    def render(self) -> None:
        """(Re-)render this view for the currently open board."""
        if self.busy:
            return
        paths = self.app.board_paths()
        if paths is None:
            self._show_message(self.app.no_board_message())
            return

        self.busy = True
        self.rendered = True
        self.reload_btn.config(state=tk.DISABLED)
        self._set_caption("rendering...")
        arg = self._selected()
        # Read the viewport HERE, on the Tk thread: a worker may not touch widgets.
        width = self._render_width()
        threading.Thread(target=self._worker, args=(paths, arg, width),
                         daemon=True).start()

    def _render_width(self) -> int:
        """Pixels to render at, sized to the viewport rather than fixed.

        Agent view is a maximised window, so the old fixed 1600 px plot (and 1400 px 3D
        render) had to be upsampled with NEAREST to fill it, which turns pad edges into
        stairs.  2x the viewport is enough for a Retina panel.

        The 3D cap is lower than the 2D one because raytracing cost is linear in pixels
        and 2D plotting is not: measured on the demo board, 2888x2072 took 9.6 s against
        ~3 s for the old 1400x1000, while a 1600 px plot renders in 0.4 s either way.
        """
        cap = 2000 if self.key == "3d" else 3200
        return int(max(1600, min(cap, max(1, self.canvas.winfo_width()) * 2)))

    def _worker(self, paths, arg, width: int) -> None:
        import time
        t0 = time.time()
        try:
            out = os.path.join(self.app.render_dir, f"{self.key}.png")
            if self.key == "pcb":
                board_views.render_pcb(paths, out, layers=arg, width=width,
                                       theme=self.app.render_theme)
            elif self.key == "3d":
                # 7:5 is the aspect the fixed 1400x1000 default used; keeping it means a
                # wider render is more pixels, not a different framing of the board.
                board_views.render_3d(paths, out, side=arg, width=width,
                                      height=int(width * 5 / 7))
            else:
                # A schematic is mostly thin lines and small text, so it keeps its own
                # higher floor rather than dropping to the plot's.
                board_views.render_schematic(paths, out, width=max(1800, width),
                                             theme=self.app.render_theme)
            dt = time.time() - t0
            self.app.post(lambda: self._loaded(out, dt))
        except board_views.NoSchematic as exc:
            msg = str(exc)
            self.app.post(lambda: self._failed(msg, soft=True))
        except Exception as exc:                      # noqa: BLE001 - shown to the user
            msg = f"{type(exc).__name__}: {exc}"
            self.app.post(lambda: self._failed(msg, soft=False))

    def _loaded(self, path: str, dt: float) -> None:
        self.busy = False
        self.reload_btn.config(state=tk.NORMAL)
        try:
            from PIL import Image, ImageTk               # noqa: PLC0415
        except ImportError:
            # Naming the fix matters more here than anywhere else in the window: the render
            # WORKED, so nothing about the board or the connection is wrong, and without the
            # remedy the reader is left with a path and no idea what to do. `phicad venv`
            # installs pillow (see phicad/bin/phicad), and ai_view.sh prefers that
            # interpreter -- so "launched with the wrong python" is the other likely cause.
            self._failed("Pillow is not installed in this interpreter, so the rendered "
                         "image cannot be displayed.\n\n"
                         "The render itself succeeded: " + path + "\n\n"
                         "Fix: run `phicad venv` to install pillow, or launch AI View "
                         "through ai_view.sh so it uses the repo's build/venv.", soft=True)
            return
        self._ImageTk = ImageTk
        try:
            self.pil_image = Image.open(path)
            self.pil_image.load()
        except Exception as exc:                        # noqa: BLE001
            self._failed(f"could not open the rendered image: {exc}", soft=False)
            return

        self.message.pack_forget()
        self.canvas.pack(fill=tk.BOTH, expand=True)
        w, h = self.pil_image.size
        self._set_caption(f"{w}x{h} px  ·  {dt:.1f}s")
        self.fit()

    def _failed(self, message: str, soft: bool) -> None:
        self.busy = False
        self.reload_btn.config(state=tk.NORMAL)
        self._set_caption("" if soft else "render failed")
        self._show_message(message, soft=soft)

    def _show_message(self, text: str, soft: bool = True) -> None:
        self.canvas.pack_forget()
        self.message.config(text=text,
                            foreground=(ui_theme.TEXT_DIM if soft
                                        else ui_theme.ON_LIGHT_RED))
        self.message.pack(fill=tk.BOTH, expand=True, padx=18, pady=16, anchor="nw")

    def describe(self) -> str:
        """What this tab is ACTUALLY showing, for a /view command to report back.

        Never guesses from the command that was just run: a `/view tab 3d` whose render
        then fails has to say the render failed, not "showing 3D".
        """
        if self.busy:
            return "rendering now"
        if self.pil_image is not None:
            iw, ih = self.pil_image.size
            _, relative = view_commands.describe_zoom(self.zoom, self.fit_zoom)
            return f"{iw}x{ih} px, zoom {self.zoom:.2f}x ({relative})"
        if self.rendered:
            first = (self.message.cget("text") or "").strip().splitlines()
            return "nothing drawn: " + (first[0] if first else "no image")
        return "not rendered yet"

    # -------------------------------------------------------------- zoom / pan

    def _viewport(self):
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def fit(self) -> None:
        if self.pil_image is None:
            return
        vw, vh = self._viewport()
        iw, ih = self.pil_image.size
        self.fit_zoom = min(vw / iw, vh / ih)
        self.zoom = self.fit_zoom
        self._redraw(center=True)

    def zoom_by(self, factor: float) -> None:
        if self.pil_image is None:
            return
        # Clamp against the fit zoom, not absolute values: a 200 KB plot and a 4000 px
        # render have very different natural scales.
        self.zoom = max(self.fit_zoom * 0.5, min(self.zoom * factor, self.fit_zoom * 12))
        self._redraw()

    def _wheel(self, event) -> None:
        self.zoom_by(1.1 if event.delta > 0 else 1 / 1.1)

    def _press(self, event) -> None:
        self._drag = (event.x, event.y)

    def _motion(self, event) -> None:
        if self._drag is None:
            return
        dx, dy = event.x - self._drag[0], event.y - self._drag[1]
        self._drag = (event.x, event.y)
        self.canvas.move("img", dx, dy)

    def _on_frame_resize(self, event) -> None:
        # Keep the message text inside the pane, with a margin for the padding.
        self.message.configure(wraplength=max(220, event.width - 40))

    def _set_caption(self, text: str) -> None:
        """The size/time readout, drawn in the canvas's bottom-left corner.

        On the canvas rather than in the toolbar because the toolbar has no room for it
        at the pane widths this window actually gets used at, and because a readout about
        the picture belongs on the picture.
        """
        self.canvas.itemconfigure(self.caption, text=text)
        self._place_caption(self.canvas.winfo_height())

    def _place_caption(self, height: int) -> None:
        self.canvas.coords(self.caption, 12, max(20, height - 12))
        if not self.canvas.itemcget(self.caption, "text"):
            self.canvas.itemconfigure(self.caption_bg, state=tk.HIDDEN)
            return
        x1, y1, x2, y2 = self.canvas.bbox(self.caption)
        self.canvas.coords(self.caption_bg, x1 - 7, y1 - 4, x2 + 7, y2 + 4)
        self.canvas.itemconfigure(self.caption_bg, state=tk.NORMAL)
        self.canvas.tag_raise(self.caption_bg)
        self.canvas.tag_raise(self.caption)

    def _on_resize(self, event) -> None:
        self._place_caption(event.height)
        # Only auto-refit while at fit zoom, so a user who zoomed in is not yanked back
        # every time the window is resized.
        if self.pil_image is not None and abs(self.zoom - self.fit_zoom) < 1e-6:
            self.fit()

    def _redraw(self, center: bool = False) -> None:
        if self.pil_image is None or self._ImageTk is None:
            return
        from PIL import Image                            # noqa: PLC0415
        iw, ih = self.pil_image.size
        w, h = max(1, int(iw * self.zoom)), max(1, int(ih * self.zoom))
        # LANCZOS when shrinking keeps thin traces visible; NEAREST when magnifying past
        # 1:1 keeps pad edges crisp instead of blurry.
        resample = Image.LANCZOS if self.zoom <= 1.0 else Image.NEAREST
        self.photo = self._ImageTk.PhotoImage(self.pil_image.resize((w, h), resample))
        self.canvas.delete("img")
        vw, vh = self._viewport()
        x, y = (vw // 2, vh // 2) if center else (vw // 2, vh // 2)
        self.canvas.create_image(x, y, image=self.photo, tags="img")
        # A new image is drawn over both of them.
        self.canvas.tag_raise(self.caption_bg)
        self.canvas.tag_raise(self.caption)


class AiViewApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._events: "queue.Queue[callable]" = queue.Queue()

        self.ctx: board_context.BoardContext | None = None
        self.kicad_version = "?"
        self.ipc_state = "connecting..."
        self.digest: str | None = None          # what we ground the AI with
        self.digest_dirty = False               # context changed since last send?
        self.busy_refresh = False
        self.busy_chat = False
        self._autosend = os.environ.get("PHICAD_AI_AUTOSEND", "").strip()

        # Layout preference, read BEFORE any widget exists so agent view can be applied
        # in one go instead of flashing the manual layout first.  ui_state computes its
        # path client-side precisely so this cannot depend on an IPC connection that may
        # never come up.
        self.ui = ui_state.load()
        self.mode = (os.environ.get("PHICAD_AI_MODE", "").strip().lower()
                     or self.ui.mode)
        if self.mode not in ui_state.MODES:
            self.mode = ui_state.MODE_MANUAL
        self.chat_open = self.ui.chat_open
        self._mode_applied = False
        self._saved_geometry = ""
        self._saved_sash = None
        self._state_warned = False

        # Headless layout verification: run these chat lines on a timer and snapshot the
        # widget geometry after each, so a claim about the layout is a measurement.
        self._probe = [c.strip() for c in
                       os.environ.get("PHICAD_AI_PROBE", "").split(";") if c.strip()]
        self._probe_ms = max(100, int(os.environ.get("PHICAD_AI_PROBE_MS", "1200")))
        self._probe_started = False
        self._probe_last = "start"
        self._probe_log = []

        # Where rendered views are cached.  One directory per process, cleaned up on exit,
        # so two AI View windows never fight over the same PNG.
        self._render_tmp = tempfile.TemporaryDirectory(prefix="phicad-aiview-")
        self.render_dir = self._render_tmp.name
        # The Altium-style dark theme this fork ships, when it is installed; kicad-cli
        # falls back to the default theme if the name is unknown, so this is safe.
        self.render_theme = os.environ.get("PHICAD_AI_THEME", "phicad-altium")

        self.backend = chat_backend.resolve_backend()
        self.session = (chat_backend.ChatSession(self.backend, workdir=_HERE)
                        if self.backend else None)
        self.bridge: host_bridge.HostChatSession | None = None
        self.history: project_history.ProjectHistory | None = None
        self.history_status = "Git waiting for a board"
        self._service_project: str | None = None
        self._services_busy = False
        self._bind_busy = False

        # Designing a NEW board, as opposed to editing the open one.  The model answers in
        # the design IR and the user commits it with /design save, so the same
        # propose-then-commit contract as board_edits covers project creation too.
        self.design = design_commands.DesignSession()
        self.last_reply = ""            # what /design save reads the design out of
        self.busy_design = False

        # One "working" indicator serves both the model turn and a build: a mark in the
        # transcript that is rewritten in place with a spinner and the elapsed seconds,
        # plus the live child so the user can stop it.  None = nothing is running.
        self._work_mark = None          # Text mark at the start of the working line
        self._work_started = 0.0
        self._work_label = ""
        self._work_note = ""            # last real progress fact, never a guess
        self._work_tick = None
        self._work_frame = 0
        self._chat_proc = None          # the phi child, for Stop
        self._cancelled = False
        self._build_watch = None        # (dir, project, [artefacts not yet seen])

        # Aqua follows the macOS appearance.  Resolve semantic system colours before
        # constructing anything so explicit status/text colours stay legible in both
        # light and dark mode instead of assuming a white shell.
        ui_theme.use_system_appearance(root)
        self.ROLE_CHIPS = {
            "user": (ui_theme.ON_LIGHT_BLUE, "#ffffff"),
            "ai": (ui_theme.TEXT, ui_theme.SURFACE),
            "system": (ui_theme.SURFACE_SUNK, ui_theme.TEXT_DIM),
            "error": (ui_theme.ON_LIGHT_RED, "#ffffff"),
            "board": (ui_theme.ON_LIGHT_CYAN, ui_theme.INK),
            "view": (ui_theme.SURFACE_SUNK, ui_theme.TEXT_DIM),
            "design": (ui_theme.ON_LIGHT_MAGENTA, ui_theme.INK),
        }
        self.fonts = ui_theme.Fonts.resolve(root)
        self._build_ui()
        self._pump()
        self._poll_project_services()
        self.refresh_context()
        if self.backend:
            threading.Thread(target=self._probe_backend, daemon=True).start()

        # KiCad starts plugin processes in the background on macOS; claim focus once.
        root.lift()
        root.attributes("-topmost", True)
        root.after(900, lambda: root.attributes("-topmost", False))

    # ------------------------------------------------------------- UI build

    def _build_ui(self) -> None:
        self.root.title("PhiCad AI View")
        self.root.geometry("1200x780")
        self.root.minsize(920, 560)

        style = ttk.Style(self.root)
        # Agent view's notebook has no tab strip: in that mode the window IS the view.
        # The segmented selector in the mode bar takes over, and it MIRRORS the notebook
        # rather than shadowing it -- <<NotebookTabChanged>> writes the variable back, so
        # the notebook stays the single source of truth for "which view is showing".
        ui_theme.apply_ttk_styles(style, self.fonts)

        # --- mode bar: in BOTH modes, because it is the only way back out of agent ---
        self.modebar = ttk.Frame(self.root, padding=(12, 7, 12, 6))
        self.modebar.pack(side=tk.TOP, fill=tk.X)

        self.mode_var = tk.StringVar(value=self.mode)
        self.mode_buttons = []
        for label, value in (("Agent", ui_state.MODE_AGENT),
                             ("Manual", ui_state.MODE_MANUAL)):
            button = ttk.Radiobutton(self.modebar, text=label, value=value,
                                     variable=self.mode_var, style="Toolbutton",
                                     command=self._on_mode_button)
            button.pack(side=tk.LEFT, padx=(0, 2))
            self.mode_buttons.append(button)

        self.mode_sep = ttk.Separator(self.modebar, orient=tk.VERTICAL)
        self.viewsel_frame = ttk.Frame(self.modebar)
        self.viewsel = tk.StringVar(value="Context")
        # Agent view hides the tab strip, so this selector is the ONLY way to reach a
        # tab there -- a new tab that is not listed here is unreachable in agent mode.
        for text, label in (("Context", "Context"), ("PCB", "PCB"),
                            ("Schematic", "Schematic"), ("3D", "3D"),
                            ("BOM", bom_view.TAB_LABEL)):
            ttk.Radiobutton(self.viewsel_frame, text=text, value=label,
                            variable=self.viewsel, style="Toolbutton",
                            command=self._on_viewsel).pack(side=tk.LEFT, padx=(0, 2))

        # Right-hand group.  Packed as one frame so that swapping the mode-specific
        # button cannot reshuffle Refresh: pack(side=RIGHT) order depends on call order,
        # and re-packing a forgotten widget would put it in the wrong place.
        right_group = ttk.Frame(self.modebar)
        right_group.pack(side=tk.RIGHT)
        self.mode_action = ttk.Frame(right_group)
        self.mode_action.pack(side=tk.LEFT, padx=(0, 10))
        self.editor_btn = ttk.Button(self.mode_action, text="Focus Editor",
                                     style="Small.TButton", command=self.focus_editor)
        self.chat_btn = ttk.Button(
            self.mode_action,
            text="Hide Chat" if self.chat_open else "Show Chat",
            style="Small.TButton",
            command=lambda: self.set_chat_open(not self.chat_open),
        )
        self.refresh_state = ttk.Label(right_group, text="", font=self.fonts.small,
                                       foreground=ui_theme.TEXT_FAINT)
        self.refresh_state.pack(side=tk.LEFT, padx=(0, 8))
        self.refresh_btn = ttk.Button(right_group, text="Refresh",
                                      style="Small.TButton",
                                      command=self.refresh_context)
        self.refresh_btn.pack(side=tk.LEFT)

        # A coloured dot carries the connection state at a glance; the words beside it
        # still say it in full, because a colour alone is not an explanation and this
        # window's whole promise is that it explains what it cannot reach.
        # tk.Canvas, not a ttk widget: aqua honours no ttk background at all.
        status_group = ttk.Frame(self.modebar)
        # Packed last so it claims only the space the fixed widgets left: pack allocates
        # in call order, and an expanding group packed first would starve them.
        status_group.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(14, 14))
        self.status_dot = tk.Canvas(status_group, width=9, height=9,
                                    highlightthickness=0, borderwidth=0)
        self.status_dot.pack(side=tk.LEFT, pady=(1, 0))
        self._dot = self.status_dot.create_oval(1, 1, 8, 8, outline="",
                                                fill=ui_theme.TEXT_FAINT)
        self.status_compact = ttk.Label(status_group, text="", font=self.fonts.small,
                                        anchor="w", foreground=ui_theme.TEXT_DIM)
        self.status_compact.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(7, 0))
        # The line is fitted to the width it is actually given, not assumed to fit: see
        # _render_status_compact.  Bound on the label so a chat toggle or a mode switch
        # re-fits it too, not just a window resize.
        self._status_segments: list[str] = []
        self.status_compact.bind("<Configure>", self._on_status_resize)

        # --- status block (the "prove the wiring" lines), manual mode only ---
        # Set in the SMALL face, not the body face: these two lines are evidence you go
        # looking for when something is wrong, not something to read every session, and
        # at body size they were the loudest thing in the window.
        self.statusbar = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        self.statusbar.pack(side=tk.TOP, fill=tk.X)
        self.status_wiring = ttk.Label(self.statusbar, font=self.fonts.small, anchor="w",
                                       foreground=ui_theme.TEXT_DIM)
        self.status_wiring.pack(side=tk.TOP, fill=tk.X)
        self.status_board = ttk.Label(self.statusbar, font=self.fonts.small, anchor="w",
                                      foreground=ui_theme.TEXT_DIM)
        self.status_board.pack(side=tk.TOP, fill=tk.X)
        self.status_sep = ttk.Separator(self.root)
        self.status_sep.pack(side=tk.TOP, fill=tk.X)

        self.panes = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.panes.pack(fill=tk.BOTH, expand=True)

        # --- left: notebook of views (text context + rendered PCB / schematic / 3D) ---
        self.left = ttk.Frame(self.panes, padding=(8, 6, 4, 8))
        self.panes.add(self.left, weight=11)

        self.viewhead = ttk.Frame(self.left)
        self.viewhead.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(self.viewhead, text="Live board views",
                  font=self.fonts.title).pack(side=tk.LEFT)

        self.views = ttk.Notebook(self.left)
        self.views.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        # Tab 1: the text digest.  This is also what grounds the AI, so it stays first --
        # it is the only tab whose content the model actually sees.
        self.ctx_tab = ttk.Frame(self.views)
        self.views.add(self.ctx_tab, text="Context")
        self.context_text = ScrolledText(self.ctx_tab, font=self.fonts.mono_small,
                                         wrap=tk.NONE, width=64,
                                         borderwidth=0, highlightthickness=0,
                                         background=ui_theme.SURFACE,
                                         foreground=ui_theme.TEXT,
                                         padx=12, pady=10,
                                         state=tk.DISABLED)
        self.context_text.pack(fill=tk.BOTH, expand=True)
        # The digest's own structure: rules are section breaks, "Label : value" rows are
        # a two-column table.  Colouring the labels is the difference between a wall of
        # monospace and something the eye can index.
        self.context_text.tag_configure("ctx_rule", foreground=ui_theme.TEXT_FAINT)
        self.context_text.tag_configure("ctx_head", foreground=ui_theme.ON_LIGHT_CYAN,
                                        font=self.fonts.mono_small)
        self.context_text.tag_configure("ctx_key", foreground=ui_theme.TEXT_DIM)

        # Tabs 2-4: rendered images.  Each is lazy -- rendering all three on startup would
        # add seconds to a window whose first job is to show the context, and the 3D render
        # is the slowest thing here by an order of magnitude.
        self.image_tabs: dict[str, ImageTab] = {}
        for key, label, spec in (
            ("pcb", "PCB", None),
            ("sch", "Schematic", None),
            ("3d", "3D", None),
        ):
            tab = ImageTab(self.views, key, self)
            self.views.add(tab.frame, text=label)
            self.image_tabs[key] = tab

        # Tab 5: the BOM and its datasheets.  Kept OUT of image_tabs deliberately -- the
        # /view zoom, layers and render verbs are all about a rendered picture, and a
        # table that answered them by duck typing would be a worse lie than not being a
        # target for them at all.  It is lazy for the same reason the others are, and it
        # owns everything it needs; see bom_view.py.
        self.bom_tab = bom_view.BomTab(self.views, self)
        self.views.add(self.bom_tab.frame, text=bom_view.TAB_LABEL)

        # Render on first reveal, not on construction.
        self.views.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Open straight to a chosen tab.  Useful in its own right ("always show me the 3D
        # view"), and it is the only way to exercise a tab without synthesising clicks --
        # ttk.Notebook tabs are not accessibility radio buttons, so AppleScript cannot
        # select them, and driving the UI that way would be the wrong test anyway.
        # The environment wins over the remembered tab: an override is for one launch.
        want = (os.environ.get("PHICAD_AI_TAB", "").strip().lower() or self.ui.tab)
        if want:
            for i in range(len(self.views.tabs())):
                if self.views.tab(i, "text").lower() == want:
                    self.views.select(i)
                    break
            else:
                names = [self.views.tab(i, "text") for i in range(len(self.views.tabs()))]
                print(f"ai-view: PHICAD_AI_TAB={want!r} is not one of {names}",
                      file=sys.stderr)

        # The floating-chat affordance for agent mode.  A child of `left` so it can be
        # placed over the render; it only ever appears when the panel is closed.
        self.chat_open_btn = ttk.Button(self.left, text="Chat",
                                        command=lambda: self.set_chat_open(True))

        # --- right: chat.  A child of the PanedWindow FOREVER (Tk cannot reparent), so
        # agent mode floats this same frame with place(in_=self.left) instead. ---
        self.chat = ttk.Frame(self.panes, padding=(4, 6, 8, 8))
        self.panes.add(self.chat, weight=9)

        chead = ttk.Frame(self.chat, padding=(2, 0, 2, 6))
        chead.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(chead, text="Chat", font=self.fonts.title).pack(side=tk.LEFT)
        self.chat_close_btn = ttk.Button(chead, text="Close", width=6,
                                         style="Small.TButton",
                                         command=lambda: self.set_chat_open(False))
        # Stop is packed only while something is running, so its absence means "nothing
        # is in flight" rather than "the button does nothing".
        self.stop_btn = ttk.Button(chead, text="Stop", width=5, style="Small.TButton",
                                   command=self.stop_work)
        self.chat_state = ttk.Label(chead, text="", font=self.fonts.small,
                                    foreground=ui_theme.TEXT_FAINT)
        self.chat_state.pack(side=tk.RIGHT)

        # The input row is built and packed BEFORE the transcript, and that order is the
        # whole point.  pack allocates in call order, and a ScrolledText asks for 24
        # lines: packed first with expand=True it swallows the cavity and the fixed-size
        # entry row below it is squeezed to a few pixels -- to nothing once agent view's
        # mode bar took another 38 px of height, leaving a chat pane with no way to type
        # in it.  Packing the fixed row first guarantees it its parcel; the transcript
        # then expands into whatever is left, which is what it wanted anyway.
        self.hint = ttk.Label(self.chat, text="", font=self.fonts.small,
                              foreground=ui_theme.TEXT_FAINT, anchor="w")
        self.hint.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))

        entry_row = ttk.Frame(self.chat)
        entry_row.pack(side=tk.BOTTOM, fill=tk.X)
        # Same trap one level down, and the same fix: the Send button is packed before
        # the expanding entry, because an entry packed first takes the whole row and
        # leaves the button unmapped -- which it was, invisibly, until measured.
        self.send_btn = ttk.Button(entry_row, text="Send", command=self.send_message)
        self.send_btn.pack(side=tk.RIGHT, padx=(8, 0), fill=tk.Y)
        # A tk.Frame is the border, and the Text inside it is borderless: aqua draws its
        # own bezel on a bordered tk.Text and it cannot be recoloured, so the only way to
        # get a hairline that matches everything else is to draw it one level up.
        entry_box = tk.Frame(entry_row, background=ui_theme.LINE,
                             highlightthickness=0, borderwidth=0)
        entry_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.entry = tk.Text(entry_box, font=self.fonts.ui, height=1, wrap=tk.WORD,
                             borderwidth=0, relief=tk.FLAT, highlightthickness=0,
                             background=ui_theme.SURFACE, foreground=ui_theme.TEXT,
                             insertbackground=ui_theme.ON_LIGHT_BLUE,
                             padx=9, pady=7, undo=True)
        self.entry.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Shift-Return>", lambda e: None)  # default = insert newline
        self.entry.bind("<KeyRelease>", self._on_entry_changed)
        self.entry.bind("<<Paste>>", lambda e: self.entry.after_idle(self._autosize_entry))
        self.entry.bind("<FocusIn>", self._on_entry_focus)
        self.entry.bind("<FocusOut>", self._on_entry_blur)
        for key, handler in (("<Up>", self._palette_up), ("<Down>", self._palette_down),
                             ("<Tab>", self._palette_accept),
                             ("<Escape>", self._palette_escape)):
            self.entry.bind(key, handler)

        self.transcript = ScrolledText(self.chat, font=self.fonts.ui, wrap=tk.WORD,
                                       borderwidth=0, relief=tk.FLAT,
                                       highlightthickness=0, state=tk.DISABLED,
                                       background=ui_theme.SURFACE,
                                       foreground=ui_theme.TEXT,
                                       padx=10, pady=8,
                                       spacing1=1, spacing3=3)
        self.transcript.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self._configure_transcript_tags()

        # The completion popup for slash commands.  place()d inside the chat frame rather
        # than being a Toplevel: an overrideredirect window does not reliably stay above
        # its parent here (measured; see the README), and making it -topmost would float
        # it over the PhiCad editor too.  place() inside the same toplevel has neither
        # problem and follows the panel when agent view moves it.
        self.palette = tk.Frame(self.chat, background=ui_theme.LINE)
        self.palette_inner = tk.Frame(self.palette, background=ui_theme.SURFACE)
        self.palette_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.palette_rows = []
        self.palette_items = []
        self.palette_index = 0
        self.palette_open = False

        if self.backend:
            self._say_system(
                "AI backend ready. Questions are grounded with the live board context "
                "on the first turn and again after every Refresh.")
        else:
            self._say_system(
                "NO AI BACKEND AVAILABLE - `phi` was not found on PATH or at "
                "~/.local/bin/phi.\n"
                "The window still shows the live board context, and Send will display "
                "exactly what WOULD have been submitted. No answers are fabricated.\n"
                "Fix: install phi and sign in. The AI harness is baked in and "
                "cannot be redirected to another command.")
        if self.ui.last_error:
            self._say_system("Saved window layout could not be read, so this window "
                             "opened with the defaults:\n" + self.ui.last_error)

        self._set_hint()
        self._show_placeholder()

        # Re-size the floating panel with the window, and let Escape close it -- a panel
        # you can open but not dismiss from the keyboard is not "small and openable".
        self.left.bind("<Configure>", self._on_left_resize)
        self.chat.bind("<Configure>", self._on_chat_resize)
        self.root.bind("<Escape>", self._on_escape)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._apply_mode(initial=True)
        self._update_status()

    # --------------------------------------------------------- transcript styling

    # role -> (chip background, chip foreground).  The chip is what makes a transcript
    # scannable at the floating panel's width, where a bold word on its own line reads
    # as just more text.
    ROLE_CHIPS = {
        "user": (ui_theme.ON_LIGHT_BLUE, "#ffffff"),
        "ai": (ui_theme.TEXT, "#ffffff"),
        "system": (ui_theme.SURFACE_SUNK, ui_theme.TEXT_DIM),
        "error": (ui_theme.ON_LIGHT_RED, "#ffffff"),
        "board": (ui_theme.ON_LIGHT_CYAN, "#ffffff"),
        "view": (ui_theme.SURFACE_SUNK, ui_theme.TEXT_DIM),
        "design": (ui_theme.ON_LIGHT_MAGENTA, "#ffffff"),
    }

    def _configure_transcript_tags(self) -> None:
        """Every tag the transcript can paint with.

        Order matters and is load-bearing: Tk resolves competing tags by creation order,
        latest wins.  The JSON token tags are therefore created after `code`, or the
        block background would repaint over its own syntax colours.
        """
        fonts = self.fonts
        text = self.transcript
        indent = 14

        for role, (bg, fg) in self.ROLE_CHIPS.items():
            text.tag_configure(f"chip_{role}", background=bg, foreground=fg,
                               font=fonts.small_bold, spacing1=10, spacing3=3)
        text.tag_configure("meta", font=fonts.small, foreground=ui_theme.TEXT_FAINT,
                           spacing1=10, spacing3=3)

        text.tag_configure("body", lmargin1=indent, lmargin2=indent, rmargin=6)
        text.tag_configure("body_dim", lmargin1=indent, lmargin2=indent, rmargin=6,
                           foreground=ui_theme.TEXT_DIM)
        text.tag_configure("body_err", lmargin1=indent, lmargin2=indent, rmargin=6,
                           foreground=ui_theme.ON_LIGHT_RED)
        text.tag_configure("bullet", lmargin1=indent, lmargin2=indent + 16, rmargin=6)
        text.tag_configure("heading", font=fonts.ui_bold, lmargin1=indent,
                           lmargin2=indent, spacing1=6, spacing3=2)
        text.tag_configure("strong", font=fonts.ui_bold)
        text.tag_configure("icode", font=fonts.mono_small,
                           background=ui_theme.SURFACE_SUNK,
                           foreground=ui_theme.ON_LIGHT_MAGENTA)
        # Preformatted output (a help table, a stage report): keeps its alignment but
        # stays on the light surface, because it is ours rather than the model's.
        # lmargin2 is deeper than lmargin1 so a wrapped row of a stage table is visibly a
        # continuation rather than a new stage.
        text.tag_configure("pre", font=fonts.mono_small, lmargin1=indent,
                           lmargin2=indent + 22, rmargin=4, spacing1=0, spacing3=0)
        text.tag_configure("gate_pass", font=fonts.mono_small,
                           foreground=ui_theme.ON_LIGHT_GREEN)
        text.tag_configure("gate_fail", font=fonts.mono_small,
                           foreground=ui_theme.ON_LIGHT_RED)
        text.tag_configure("work", lmargin1=indent, lmargin2=indent,
                           foreground=ui_theme.TEXT_DIM, font=fonts.small)
        text.tag_configure("help_summary", lmargin1=indent + 12,
                           lmargin2=indent + 12, rmargin=6, spacing3=5,
                           foreground=ui_theme.TEXT_DIM, font=fonts.small)

        text.tag_configure("code_caption", font=fonts.small,
                           foreground=ui_theme.TEXT_FAINT,
                           lmargin1=indent, lmargin2=indent, spacing1=8, spacing3=2)
        text.tag_configure("code", font=fonts.mono_small, background=ui_theme.CODE_BG,
                           foreground=ui_theme.CODE_FG, lmargin1=indent,
                           lmargin2=indent + 18, rmargin=4, spacing1=0, spacing3=0)
        text.tag_configure("code_rule", font=fonts.mono_small,
                           background=ui_theme.ON_LIGHT_MAGENTA)
        for name, colour in (("key", ui_theme.CODE_KEY), ("str", ui_theme.CODE_STR),
                             ("num", ui_theme.CODE_NUM), ("lit", ui_theme.CODE_LIT),
                             ("punc", ui_theme.CODE_PUNC)):
            text.tag_configure(f"code_{name}", foreground=colour,
                               background=ui_theme.CODE_BG, font=fonts.mono_small)

    # ------------------------------------------------------------- saying things

    def _say(self, role_label: str, text: str, kind: str = "system",
             meta: str = "", pre: bool = False) -> None:
        """Append one message: a role chip, an optional meta suffix, then the body.

        Everything goes through here so that a board edit, a model answer and a failure
        are laid out by the same rules -- a previous version styled errors with the same
        grey as routine chatter, which is how a discarded edit read as small talk.

        While something is running, the live "working" line is the last thing in the
        transcript.  It is taken down before the message and put back after, rather than
        inserted around: a Tk mark with right gravity looked like the tidy way to do it
        and left a copy of the working line behind on every tick, because an insert at
        END is not an insert AT the mark and the mark never moved.  Removing and
        re-opening needs no gravity reasoning at all.
        """
        working = self._work_mark is not None
        if working:
            self._close_work_line()
        self.transcript.config(state=tk.NORMAL)
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert(tk.END, "\n")
        chip = f"chip_{kind}" if kind in self.ROLE_CHIPS else "chip_system"
        self.transcript.insert(tk.END, f" {role_label} ", chip)
        stamp = meta or time.strftime("%H:%M")
        self.transcript.insert(tk.END, f"  {stamp}\n", "meta")
        self._insert_body(text, kind, pre)
        self.transcript.config(state=tk.DISABLED)
        self.transcript.see(tk.END)
        if working:
            self._open_work_line()

    def _insert_body(self, text: str, kind: str, pre: bool) -> None:
        """Lay out one message body: fenced blocks as cards, the rest as prose."""
        base = {"error": "body_err", "system": "body_dim",
                "view": "body_dim"}.get(kind, "body")
        for block in ui_theme.split_blocks(text.rstrip()):
            if block.kind == "code":
                self._insert_code(block)
            elif pre:
                for line in block.lines:
                    # The gate verdict is the compiler's own first word; colouring it is
                    # the difference between reading a stage table and scanning one.
                    verdict = line[:4]
                    if verdict in ("PASS", "FAIL"):
                        self.transcript.insert(tk.END, verdict,
                                               ("pre", f"gate_{verdict.lower()}"))
                        self.transcript.insert(tk.END, line[4:] + "\n", "pre")
                    else:
                        self.transcript.insert(tk.END, line + "\n", "pre")
            else:
                self._insert_prose(block.lines, base)

    def _insert_prose(self, lines, base: str) -> None:
        for raw in lines:
            kind, prefix, spans = ui_theme.prose_line(raw)
            tag = {"heading": "heading", "bullet": "bullet"}.get(kind, base)
            if prefix:
                self.transcript.insert(tk.END, prefix, (tag, "body_dim"))
            for span in spans:
                extra = {"strong": "strong", "code": "icode"}.get(span.style)
                tags = (tag, extra) if extra else (tag,)
                self.transcript.insert(tk.END, span.text, tags)
            self.transcript.insert(tk.END, "\n", tag)

    def _code_width(self) -> int:
        """How many monospace columns fit across the transcript right now.

        Code lines are padded to this so the block paints as a rectangle instead of a
        ragged silhouette -- and padded to THIS rather than to the longest line, because
        padding past the wrap point turns every line into two, one of them blank.  In
        the floating panel that is the difference between a card and a mess.
        """
        try:
            from tkinter import font as tkfont                # noqa: PLC0415

            char = tkfont.Font(font=self.fonts.mono_small).measure("0") or 7
        except Exception:                                      # noqa: BLE001
            char = 7
        usable = max(120, self.transcript.winfo_width() - 60)
        return max(24, min(160, usable // char))

    def _insert_code(self, block) -> None:
        """One fenced block as a card: caption, coloured left rule, monospace body."""
        self.transcript.insert(tk.END, ui_theme.code_caption(block) + "\n",
                               "code_caption")
        width = self._code_width()
        json_block = block.is_json()
        for line in block.lines:
            self.transcript.insert(tk.END, " ", "code_rule")
            padded = line.ljust(width)
            if json_block:
                for span in ui_theme.json_spans(padded):
                    style = f"code_{span.style}" if span.style else None
                    self.transcript.insert(tk.END, span.text,
                                           ("code", style) if style else ("code",))
            else:
                self.transcript.insert(tk.END, padded, "code")
            self.transcript.insert(tk.END, "\n", "code")

    def _say_error(self, role_label: str, text: str) -> None:
        self._say(role_label, text, "error")

    # ------------------------------------------------------------- working state

    def _start_work(self, label: str, note: str = "", stoppable: bool = False) -> None:
        """Show a live "working" line: spinner, elapsed seconds, and the latest fact.

        A turn takes seconds and a build takes tens of them, and the old window said
        "AI is thinking..." once and then nothing -- which is indistinguishable from a
        window that has hung.  The seconds ticking are the proof that it has not.
        """
        self._work_started = time.time()
        self._work_label = label
        self._work_note = note
        self._work_frame = 0
        if stoppable:
            self.stop_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._open_work_line()
        self._tick_work()

    def _open_work_line(self) -> None:
        """Lay down the line the timer will rewrite, and remember where it starts.

        The line and its newline are written ONCE and every tick then replaces only the
        characters between linestart and lineend.  Delete-to-END was the obvious way and
        is wrong: `delete(i, "end")` also removes the newline that ends the line before
        i, so the anchor collapses upward by one line and the next insert lands after the
        previous text instead of over it -- which showed up as a transcript accumulating
        one copy of "Compiling the design 0s" per tick, four a second.  Rewriting inside
        a line never moves anything.
        """
        self.transcript.config(state=tk.NORMAL)
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert(tk.END, "\n")
        self._work_mark = self.transcript.index("end-1c")
        self.transcript.insert(self._work_mark, " \n", "work")
        self.transcript.config(state=tk.DISABLED)

    def _close_work_line(self) -> None:
        """Take the line down, along with the blank line that separated it."""
        if self._work_mark is None:
            return
        self.transcript.config(state=tk.NORMAL)
        self.transcript.delete(self._work_mark, tk.END)
        self.transcript.config(state=tk.DISABLED)

    def _tick_work(self) -> None:
        if self._work_mark is None:
            return
        self._work_frame += 1
        spin = SPINNER[self._work_frame % len(SPINNER)]
        elapsed = time.time() - self._work_started
        line = f"{spin}  {self._work_label}   {elapsed:.0f}s"
        if self._work_note:
            line += f"   ·   {self._work_note}"
        self.transcript.config(state=tk.NORMAL)
        self.transcript.delete(self._work_mark, self._work_mark + " lineend")
        self.transcript.insert(self._work_mark, line, "work")
        self.transcript.config(state=tk.DISABLED)
        self.transcript.see(tk.END)
        self._work_tick = self.root.after(TICK_MS, self._tick_work)

    def _finish_work(self) -> float:
        """Remove the working line.  Returns how long it ran, for the reply's meta."""
        if self._work_tick is not None:
            self.root.after_cancel(self._work_tick)
            self._work_tick = None
        elapsed = time.time() - self._work_started if self._work_mark else 0.0
        self._close_work_line()
        self._work_mark = None
        self.stop_btn.pack_forget()
        self._work_note = ""
        return elapsed

    def stop_work(self) -> None:
        """Stop the running model turn.

        Only the model turn: a build is a pipeline that writes files, and killing it
        half way leaves a project directory nobody can trust.  The button is packed only
        while something stoppable is running, so it never lies about what it can do.
        """
        proc = self._chat_proc
        if proc is None:
            return
        self._cancelled = True
        try:
            proc.kill()
        except Exception as exc:                                   # noqa: BLE001
            self._say_error("Stop", f"could not stop the turn: {exc}")

    # -------------------------------------------------------------- the entry box

    # Short enough to survive the floating panel: measured, the full sentence clipped to
    # "Ask about the board, or type / for" at 453 px, and a truncated prompt is worse
    # than a terse one.
    PLACEHOLDER = "Ask about the board, or / for commands"

    def _show_placeholder(self) -> None:
        """Grey prompt text in an empty box.

        A real inserted string rather than a drawn overlay, because tk.Text has no
        placeholder option; _clear_placeholder and send_message both know to treat it as
        empty, so it can never be sent.
        """
        if self.entry.get("1.0", tk.END).strip():
            return
        self._placeholder_on = True
        self.entry.delete("1.0", tk.END)
        self.entry.insert("1.0", self.PLACEHOLDER)
        self.entry.tag_add("ph", "1.0", tk.END)
        self.entry.tag_configure("ph", foreground=ui_theme.TEXT_FAINT)

    def _clear_placeholder(self) -> None:
        if getattr(self, "_placeholder_on", False):
            self._placeholder_on = False
            self.entry.delete("1.0", tk.END)

    def _on_entry_focus(self, _event=None) -> None:
        self._clear_placeholder()

    def _on_entry_blur(self, _event=None) -> None:
        self._show_placeholder()
        self._hide_palette()

    def entry_text(self) -> str:
        if getattr(self, "_placeholder_on", False):
            return ""
        return self.entry.get("1.0", tk.END).strip()

    def _autosize_entry(self) -> None:
        """Grow the box with its content, between one line and eight.

        Fixed at three lines it wasted two thirds of the floating panel's input row when
        empty and hid everything past line three of a pasted design.  `displaylines`
        counts what is on screen after wrapping, so a single pasted paragraph grows the
        box the same way three typed lines do.
        """
        try:
            # count() returns the number of line BREAKS between the two indices, so a
            # three-line paste answers 2.  Without the +1 the box was always one row
            # short and the last line of anything pasted was invisible.
            shown = int(self.entry.count("1.0", "end-1c", "displaylines")[0]) + 1
        except Exception:                                          # noqa: BLE001
            shown = self.entry.get("1.0", tk.END).count("\n") or 1
        # Eight rows is fine in the manual split and eats the floating panel, where the
        # transcript is only ~540 px to begin with.
        limit = 8 if self.chat.winfo_height() > 560 else 5
        self.entry.configure(height=max(1, min(limit, shown)))

    def _on_entry_changed(self, event=None) -> None:
        if event is not None and event.keysym in ("Up", "Down", "Tab", "Escape"):
            return
        self._autosize_entry()
        self._update_palette()

    # --------------------------------------------------------- command completion

    def _update_palette(self) -> None:
        """Filter the command table against what has been typed and show the matches."""
        text = self.entry.get("1.0", "end-1c")
        if getattr(self, "_placeholder_on", False) or not text.startswith("/") \
                or "\n" in text:
            self._hide_palette()
            return
        typed = text.lower()
        matches = [item for item in ALL_COMMANDS
                   if (item[0] + " " + item[1]).lower().startswith(typed)
                   or item[0].lower().startswith(typed)]
        if not matches:
            # A prefix that matches nothing still deserves the full list: the user is
            # exploring, and an empty popup teaches them nothing.
            matches = [item for item in ALL_COMMANDS
                       if typed.strip("/") and typed.strip("/") in item[0].lower()]
        if not matches:
            self._hide_palette()
            return
        self.palette_items = matches[:8]
        self.palette_index = min(self.palette_index, len(self.palette_items) - 1)
        self._render_palette()
        self._place_palette()

    def _render_palette(self) -> None:
        """Draw the filtered list.

        In the floating panel the summary column does not fit -- measured, the rows want
        552 px against the panel's 441 and "keep the design JSON from the assis" is what
        a user actually saw.  So there the rows are commands only and the selected one's
        summary goes in the hint line under the box, which has the width for it.
        """
        for row in self.palette_rows:
            row[0].destroy()
        self.palette_rows = []
        roomy = self.chat.winfo_width() > 520
        for index, (name, args, summary) in enumerate(self.palette_items):
            selected = index == self.palette_index
            bg = ui_theme.SURFACE_SUNK if selected else ui_theme.SURFACE
            row = tk.Frame(self.palette_inner, background=bg)
            row.pack(fill=tk.X)
            usage = tk.Label(row, text=f"{name} {args}".strip(), background=bg,
                             foreground=ui_theme.ON_LIGHT_MAGENTA if selected
                             else ui_theme.TEXT,
                             font=self.fonts.mono_small, anchor="w", padx=8, pady=2)
            usage.pack(side=tk.LEFT)
            hint = tk.Label(row, text=summary if roomy else "", background=bg,
                            foreground=ui_theme.TEXT_FAINT, font=self.fonts.small,
                            anchor="w", padx=6, pady=2)
            hint.pack(side=tk.LEFT, fill=tk.X, expand=True)
            for widget in (row, usage, hint):
                widget.bind("<Button-1>",
                            lambda _e, i=index: self._palette_pick(i))
            self.palette_rows.append((row, usage, hint))
        # The summary only.  The usage is already the highlighted row two lines above,
        # and repeating it here cost the width that clipped the summary instead.
        self._set_hint(self.palette_items[self.palette_index][2])

    def _place_palette(self) -> None:
        """Sit the popup directly above the entry row, inside the chat frame.

        Measured from the chat frame's own height rather than from the entry's position:
        in agent view the panel is re-placed on every window resize, and a popup pinned
        to a stale coordinate ends up off the panel entirely.
        """
        self.palette.place(in_=self.chat, x=0, relwidth=1.0,
                           y=self.chat.winfo_height() - self.entry.winfo_height()
                           - self.hint.winfo_height() - 26,
                           anchor="sw")
        self.palette.lift()
        self.palette_open = True

    def _hide_palette(self) -> None:
        if self.palette_open:
            self.palette.place_forget()
            self.palette_open = False
            self._set_hint()
        self.palette_index = 0

    def _on_chat_resize(self, _event=None) -> None:
        if self.palette_open:
            self._place_palette()
        self._set_hint(getattr(self, "_hint_usage", ""))

    def _palette_move(self, delta: int) -> str:
        if not self.palette_open:
            return ""
        self.palette_index = ((self.palette_index + delta)
                              % max(1, len(self.palette_items)))
        self._render_palette()
        return "break"

    def _palette_up(self, _event=None) -> str:
        return self._palette_move(-1)

    def _palette_down(self, _event=None) -> str:
        return self._palette_move(1)

    def _palette_pick(self, index: int) -> None:
        self.palette_index = index
        self._palette_accept()

    def _palette_accept(self, _event=None) -> str:
        """Tab (or a click) completes to the selected command.

        Completes to the NAME only, never the argument spelling: "<REF>" is a
        description of what to type, and pasting it into the box would produce a command
        that fails on its own placeholder.
        """
        if not self.palette_open or not self.palette_items:
            return ""
        name, args, _ = self.palette_items[self.palette_index]
        self.entry.delete("1.0", tk.END)
        self.entry.insert("1.0", name + (" " if args else ""))
        self.entry.mark_set(tk.INSERT, tk.END)
        self._hide_palette()
        self._autosize_entry()
        self._set_hint(f"{name} {args}".strip())
        return "break"

    def _palette_escape(self, _event=None) -> str:
        if self.palette_open:
            self._hide_palette()
            return "break"
        return ""

    def _set_hint(self, usage: str = "") -> None:
        """The one line under the box that says what the keys do.

        Two lengths, chosen by the panel's actual width: the floating panel is 380-520 px
        and the full line needs about 400, so at the small end it drops to the half a
        user who has not found the commands yet still needs.
        """
        self._hint_usage = usage
        if usage:
            self.hint.config(text=usage, foreground=ui_theme.TEXT_DIM)
            return
        wide = self.chat.winfo_width() > 520
        self.hint.config(
            text=("Return sends  ·  Shift-Return newline  ·  / for commands  ·  /help"
                  if wide else "/ for commands  ·  /help"),
            foreground=ui_theme.TEXT_FAINT)

    # ------------------------------------------------------------ modes

    def set_mode(self, mode: str) -> str:
        """Switch between Agent view and Manual.  Returns what the window now shows.

        Idempotent, and the return value is read back out of the widgets rather than
        assumed, so a caller (a /mode command, the toggle, the probe) reports the layout
        that actually happened.
        """
        if mode == "toggle":
            mode = (ui_state.MODE_MANUAL if self.mode == ui_state.MODE_AGENT
                    else ui_state.MODE_AGENT)
        if mode not in ui_state.MODES:
            mode = ui_state.MODE_MANUAL
        if mode != self.mode or not self._mode_applied:
            self.mode = mode
            self.mode_var.set(mode)
            self._apply_mode()
            self._persist()
        return self.describe_layout()

    def _on_mode_button(self) -> None:
        self.set_mode(self.mode_var.get())

    def _apply_mode(self, initial: bool = False) -> None:
        if self.mode == ui_state.MODE_AGENT:
            self._enter_agent(initial=initial)
        else:
            self._enter_manual(initial=initial)
        self._mode_applied = True
        self._update_status()

    def _enter_agent(self, initial: bool = False) -> None:
        """Give the whole window to the rendered view; float the chat over it.

        Order matters: the chat leaves the PanedWindow before it is placed, or Tk has it
        managed twice; and the window is zoomed last so the panel is sized against the
        pane it will actually live in.
        """
        if not initial:
            self._saved_geometry = self.root.geometry()
            try:
                self._saved_sash = self.panes.sashpos(0)
            except tk.TclError:
                self._saved_sash = None

        if str(self.chat) in self.panes.panes():
            self.panes.forget(self.chat)
        self.chat.configure(style="Panel.TFrame")
        self.chat_close_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.statusbar.pack_forget()
        self.status_sep.pack_forget()
        self.viewhead.pack_forget()
        self.left.configure(padding=0)
        self.views.configure(style="Agent.TNotebook")

        self.mode_sep.pack(side=tk.LEFT, fill=tk.Y, padx=8,
                           after=self.mode_buttons[-1])
        self.viewsel_frame.pack(side=tk.LEFT, after=self.mode_sep)
        self.editor_btn.pack_forget()
        self.chat_btn.pack(side=tk.LEFT)

        self._maximise()
        self._apply_chat_panel()

    def _enter_manual(self, initial: bool = False) -> None:
        """Put the original split back: status block, views, chat across a draggable sash."""
        self.chat.place_forget()
        self.chat_open_btn.place_forget()
        self.chat.configure(style="TFrame")
        self.chat_close_btn.pack_forget()

        self.views.configure(style="TNotebook")
        self.left.configure(padding=(8, 6, 4, 8))
        self.viewhead.pack(side=tk.TOP, fill=tk.X, before=self.views)
        self.statusbar.pack(side=tk.TOP, fill=tk.X, before=self.panes)
        self.status_sep.pack(side=tk.TOP, fill=tk.X, before=self.panes)

        self.mode_sep.pack_forget()
        self.viewsel_frame.pack_forget()
        self.chat_btn.pack_forget()
        self.editor_btn.pack(side=tk.LEFT)

        self._unmaximise(initial=initial)

        if str(self.chat) not in self.panes.panes():
            self.panes.insert("end", self.chat, weight=9)
        self.root.after_idle(self._restore_sash)

    def _restore_sash(self, attempts: int = 6) -> None:
        """Put the divider back where the user had it, and CHECK that it went there.

        Measured: a sashpos written before the PanedWindow has laid out the re-inserted
        pane is silently dropped -- the write returns cleanly and the sash stays pinned
        to the right edge, so the chat comes back as a zero-width pane and looks lost.
        Hence read-back and retry rather than a single call.
        """
        width = self.panes.winfo_width()
        # No remembered sash means this window has never been in manual mode (it was
        # launched straight into agent view), so fall back to the 11:9 split add() uses.
        target = self._saved_sash or int(max(320, width) * 11 / 20)
        try:
            if abs(self.panes.sashpos(0) - target) > 2:
                self.panes.sashpos(0, target)
        except tk.TclError:
            return
        if attempts > 1:
            # Keep checking for a few frames rather than trusting the read-back: the
            # window is still being resized back down by the window manager, and the
            # relayout that lands afterwards re-pins the sash to the right edge.
            self.root.after(60, lambda: self._restore_sash(attempts - 1))

    def _maximise(self) -> None:
        """Fill the display, keeping the window's chrome and its Space.

        `state("zoomed")` over `-fullscreen` is a measured choice, not a preference --
        see the module docstring.  PHICAD_AI_FULLSCREEN=1 asks for the chrome-free
        version anyway, and is honoured only if Tk accepts it.
        """
        if os.environ.get("PHICAD_AI_FULLSCREEN", "").strip() in ("1", "true", "yes"):
            try:
                self.root.attributes("-fullscreen", True)
                return
            except tk.TclError:
                pass
        try:
            self.root.state("zoomed")
        except tk.TclError:
            # X11 Tk has no "zoomed" state; fall back to filling the screen by geometry.
            self.root.geometry(f"{self.root.winfo_screenwidth()}x"
                               f"{self.root.winfo_screenheight()}+0+0")

    def _unmaximise(self, initial: bool = False) -> None:
        try:
            self.root.attributes("-fullscreen", False)
        except tk.TclError:
            pass
        try:
            if self.root.state() == "zoomed":
                self.root.state("normal")
        except tk.TclError:
            pass
        # -fullscreen does NOT restore the previous geometry when cleared, so say it
        # explicitly.  Skipped on the first build, where there is nothing to restore.
        if not initial and self._saved_geometry:
            self.root.geometry(self._saved_geometry)

    # ------------------------------------------------------- floating chat panel

    def set_chat_open(self, want_open: bool) -> str:
        """Open or close the floating chat panel.  Returns what actually changed."""
        self.chat_open = bool(want_open)
        self.chat_btn.config(text="Hide Chat" if self.chat_open else "Show Chat")
        if self.chat_open:
            self.chat_open_btn.config(text="Chat")     # clear the unread marker
        self._apply_chat_panel()
        self._persist()
        if self.mode != ui_state.MODE_AGENT:
            return ("noted for agent view: in Manual mode the chat is one half of the "
                    "split and is always visible.")
        return self.describe_layout()

    def _apply_chat_panel(self) -> None:
        """Place (or unplace) the chat over the render.  Agent mode only."""
        if self.mode != ui_state.MODE_AGENT:
            return
        if self.chat_open:
            self.chat_open_btn.place_forget()
            width, height = self._chat_panel_size()
            self.chat.place(in_=self.left, relx=1.0, rely=1.0, anchor="se",
                            x=-16, y=-16, width=width, height=height)
            self.chat.lift()
        else:
            self.chat.place_forget()
            self.chat_open_btn.place(in_=self.left, relx=1.0, rely=1.0, anchor="se",
                                     x=-16, y=-16)
            self.chat_open_btn.lift()

    def _chat_panel_size(self) -> "tuple[int, int]":
        """A panel that is small next to the view but still usable as a chat.

        Proportional rather than fixed, because "small" has to mean small relative to the
        view the mode exists to make huge.  The floor is 380 px and not the old 340: the
        thing most often read in this panel is a design IR, and measured with the mono
        face a 340 px panel fits 34 monospace columns, which wraps `"footprint":
        "Package_LGA:..."` three times.  380 fits 42 and the caption line, which is where
        a JSON line starts being followable.
        """
        pane_w = max(1, self.left.winfo_width())
        pane_h = max(1, self.left.winfo_height())
        width = int(min(520, max(380, pane_w * 0.30)))
        height = int(min(640, max(320, pane_h * 0.62)))
        # Never let the panel eat the view it floats over.
        return (min(width, max(260, pane_w - 64)), min(height, max(220, pane_h - 64)))

    def _on_left_resize(self, _event=None) -> None:
        if self.mode == ui_state.MODE_AGENT and self.chat_open:
            self._apply_chat_panel()

    def _on_escape(self, _event=None) -> None:
        if self.mode == ui_state.MODE_AGENT and self.chat_open:
            self.set_chat_open(False)

    def _on_viewsel(self) -> None:
        """The mode bar's view buttons.  The notebook stays the source of truth."""
        label = self.viewsel.get()
        for index in range(len(self.views.tabs())):
            if self.views.tab(index, "text") == label:
                self.views.select(index)
                return

    # ------------------------------------------------------------ persistence

    def _persist(self) -> None:
        """Write the layout preference.  A failure costs a preference, never the window."""
        try:
            tab = self.views.tab(self.views.select(), "text").lower()
        except tk.TclError:
            tab = ""
        error = ui_state.save(ui_state.UiState(mode=self.mode,
                                               chat_open=self.chat_open, tab=tab))
        if error and not self._state_warned:
            # Once only: a read-only home would otherwise print on every mode switch.
            self._state_warned = True
            self._say_system(error + "\nThe mode still works; it just will not be "
                                     "remembered next time.")

    def describe_layout(self) -> str:
        """What the window is showing, read out of the widgets.  Used by /view state."""
        try:
            tab = self.views.tab(self.views.select(), "text")
        except tk.TclError:
            tab = "(none)"
        if self.mode == ui_state.MODE_AGENT:
            chat = ("floating, open" if self.chat_open
                    else "closed (press Chat, or /chat open)")
            shape = "agent view: the render fills the window, no tab strip"
        else:
            chat = "docked in the right half of the split"
            shape = "manual: status block, views and chat across a draggable sash"
        lines = [f"mode: {self.mode} -- {shape}",
                 f"showing: {tab}",
                 f"chat: {chat}"]
        key = self._current_image_key()
        if key:
            lines.append(f"{tab} tab: {self.image_tabs[key].describe()}")
        elif tab == bom_view.TAB_LABEL:
            lines.append(f"{tab} tab: {self.bom_tab.describe()}")
        lines.append(f"window: {self.root.geometry()}")
        return "\n".join(lines)

    # ------------------------------------------------------------ event pump

    def _post(self, fn) -> None:
        self._events.put(fn)

    # Public alias: ImageTab worker threads hand their results back through this.
    def post(self, fn) -> None:
        self._events.put(fn)

    # ------------------------------------------------------------- view tabs

    def board_paths(self):
        """Resolve the open board to a board_views.Paths, or None if no board is open.

        The API reports the project directory and the board's file name separately, so
        they are rejoined here.  A board opened outside a project still reports a path.
        """
        if not (self.ctx and self.ctx.has_board() and self.ctx.board_name):
            return None
        base = self.ctx.project_path or ""
        full = os.path.join(base, self.ctx.board_name) if base else self.ctx.board_name
        if not os.path.isfile(full):
            return None
        return board_views.Paths(full)

    def no_board_message(self) -> str:
        """Why there is nothing to draw, and what would actually fix it.

        board_paths() returns None for four different reasons and a view cannot tell them
        apart, so every empty view used to print "Open a board in PhiCad, then press
        Refresh". With a dead IPC client that advice is simply wrong -- PhiCad was running
        and a board was open; the real blocker was `No module named 'kipy'`, which the
        Context tab diagnosed correctly while the PCB tab sent the reader to the editor.
        Guessing wrong about the fix costs more than saying less.

        Each branch names the state this window is actually in. The Context tab keeps the
        full traceback and hints; these are the short form for a render pane.
        """
        if self.ctx is None:
            state = self.ipc_state or "not connected"
            if "error" in state or "failed" in state:
                return (f"Cannot read the board: IPC {state}.\n\n"
                        "See the Context tab for the exact error and how to fix it —"
                        " this is a connection problem, not a missing board.")
            return (f"Not connected to PhiCad yet (IPC {state}).\n\n"
                    "Press Refresh once the editor is up.")
        if not self.ctx.has_board():
            host = getattr(self.ctx, "host", "") or ""
            if host and "sch" in host.lower():
                return ("The schematic editor answered, and this project has no saved"
                        " board yet.\n\nCreate or open the PCB, then press Refresh.")
            return ("Connected to PhiCad, but no board is open.\n\n"
                    "Open a board in PhiCad, then press Refresh.")
        # A board is open but its file is not on disk where the API says it is: almost
        # always an unsaved new board, which cannot be plotted from a file.
        name = self.ctx.board_name or "the board"
        return (f"{name} has never been saved, so there is no file to draw from.\n\n"
                "Save it in PhiCad, then press Refresh.")

    def _clear_project_services(self) -> None:
        self._service_project = None
        self.bridge = None
        self.history = None
        self.history_status = "Git waiting for a board"
        self.session = (chat_backend.ChatSession(self.backend, workdir=_HERE)
                        if self.backend else None)

    def _bind_project_services(self) -> None:
        """Attach Git history and, when registered, a shared host phi session."""
        paths = self.board_paths()
        if paths is not None:
            project = paths.project_dir
        elif self.ctx and self.ctx.project_path and os.path.isdir(self.ctx.project_path):
            # Schematic-first project (no board file saved yet): history and the
            # host bridge are project-level services, so they bind anyway.
            project = self.ctx.project_path
        else:
            return
        if self._bind_busy or project == self._service_project:
            return
        self._bind_busy = True

        def worker() -> None:
            history = project_history.ProjectHistory(project)
            try:
                history_status = history.ensure().display()
                history_error = ""
            except project_history.HistoryError as exc:
                history_status = "Git unavailable"
                history_error = str(exc)
            try:
                descriptor = host_bridge.load(project)
                bridge_error = ""
            except host_bridge.BridgeError as exc:
                descriptor = None
                bridge_error = str(exc)
            self._post(lambda: apply(history, history_status, history_error,
                                     descriptor, bridge_error))

        def apply(history, history_status: str, history_error: str,
                  descriptor, bridge_error: str) -> None:
            self._bind_busy = False
            # The user can switch boards while the worker is running.  Never attach the
            # old project's terminal or Git repository to the new one.
            current = self.board_paths()
            if current is None or current.project_dir != project:
                self._bind_project_services()
                return
            self._service_project = project
            self.history = history
            self.history_status = history_status
            if history_error:
                self._say_error("Git", history_error)

            if descriptor is not None:
                self.bridge = host_bridge.HostChatSession(descriptor)
                self.session = self.bridge
                self._say_system(
                    "Connected to a shared phi session. Messages sent "
                    "from either window appear in both; the host process remains "
                    "the only session writer.")
                for message in self.bridge.history():
                    self._show_shared_message(message)
            else:
                self.bridge = None
                self.session = (chat_backend.ChatSession(self.backend, workdir=project)
                                if self.backend else None)
                if bridge_error:
                    self._say_error("Session", bridge_error)
            self._update_status()

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _shared_user_text(text: str) -> str:
        """Hide the transport envelope/context when mirroring an Agent View turn."""
        if not text.startswith("[PhiCad Agent View | phicad-agent-view:"):
            return text
        body = text.split("\n", 1)[1] if "\n" in text else text
        separator = "\n\nUse the live PhiCad board context below as authoritative"
        return body.split(separator, 1)[0].strip()

    def _show_shared_message(self, message: host_bridge.SessionMessage) -> None:
        if message.role == "user":
            self._say("You", self._shared_user_text(message.text), "user")
        else:
            self.last_reply = message.text
            self._say("AI", message.text, "ai")
        if self.mode == ui_state.MODE_AGENT and not self.chat_open:
            self.chat_open_btn.config(text="Chat *")

    def _poll_project_services(self) -> None:
        """Mirror shared turns and commit stable project changes without blocking Tk."""
        self.root.after(1500, self._poll_project_services)
        if self._services_busy:
            return
        history = self.history
        bridge = self.bridge if not self.busy_chat else None
        project = self._service_project
        if history is None and bridge is None and project is None:
            return
        self._services_busy = True

        def worker() -> None:
            messages = []
            history_status = self.history_status
            # Watch the bridge descriptor for the transitions this window cannot
            # cause itself: a host creating it, a disconnect removing
            # it, or a reconnect pointing it at a different phi session.  Without
            # this, attaching required reopening the window — the bind guard keys
            # on the project and never looks at the descriptor again.
            drift = ""
            if project is not None:
                try:
                    descriptor = host_bridge.load(project)
                except host_bridge.BridgeError:
                    # Unusable descriptor (e.g. its phi session file vanished).  If
                    # attached to it, rebinding detaches and reports; if not, the
                    # bind path already reported it once — stay quiet.
                    descriptor = None
                    if bridge is not None:
                        drift = "lost"
                else:
                    if bridge is None and descriptor is not None:
                        drift = "appeared"
                    elif bridge is not None and descriptor is None:
                        drift = "removed"
                    elif (bridge is not None and descriptor is not None
                          and descriptor.session_id != bridge.descriptor.session_id):
                        drift = "replaced"
            try:
                if bridge is not None and not drift:
                    messages = bridge.poll()
                if history is not None:
                    history.autosnapshot()
                    history_status = history.status().display()
                error = ""
            except (host_bridge.BridgeError, project_history.HistoryError) as exc:
                error = str(exc)
            self._post(lambda: apply_drift(drift) if drift
                       else apply(messages, history_status, error))

        def apply_drift(drift: str) -> None:
            self._services_busy = False
            if project != self._service_project:
                return                       # the project changed under the poll
            if drift in ("removed", "lost"):
                self._say_system(
                    "The shared phi session was disconnected"
                    + (" (its phi session is gone)" if drift == "lost" else "")
                    + "; this window is back on its own local phi conversation.")
            # Rebind from scratch: the bind path owns attach announcements, history
            # replay and session construction, so the poll never duplicates it.
            self._service_project = None
            self._bind_project_services()

        def apply(messages, history_status: str, error: str) -> None:
            self._services_busy = False
            if history is not self.history or (bridge is not None and bridge is not self.bridge):
                return
            for message in messages:
                self._show_shared_message(message)
            self.history_status = history_status
            if error:
                self.history_status = "Git/session attention needed"
                self._say_error("Project", error)
            self._update_status()

        threading.Thread(target=worker, daemon=True).start()

    def _on_tab_changed(self, _event=None) -> None:
        """Render a view the first time its tab is actually shown.

        Lazy on purpose: a 3D render costs ~3 s, and paying that on window open for a tab
        the user may never look at would make the whole plugin feel slow.
        """
        try:
            label = self.views.tab(self.views.select(), "text")
        except tk.TclError:
            return
        # Mirror the selection into the mode bar's selector, which is the only way to
        # change view in agent mode (the tab strip is hidden there).
        if self.viewsel.get() != label:
            self.viewsel.set(label)
        if self._mode_applied:
            self._persist()
        if label == bom_view.TAB_LABEL and not self.bom_tab.rendered:
            self.bom_tab.render()
            return
        for key, tab in self.image_tabs.items():
            if self.views.tab(tab.frame, "text") == label and not tab.rendered:
                tab.render()
                break

    def _current_image_key(self) -> str:
        """The ImageTab key currently on screen, or "" for the text Context tab."""
        try:
            current = self.views.select()
        except tk.TclError:
            return ""
        for key, tab in self.image_tabs.items():
            if str(tab.frame) == str(current):
                return key
        return ""

    def _invalidate_views(self) -> None:
        """Mark rendered views stale after the board context changed.

        The visible tab is re-rendered immediately; the others are just marked, so they
        re-render when next revealed.
        """
        try:
            current = self.views.tab(self.views.select(), "text")
        except (tk.TclError, AttributeError):
            return
        for tab in self.image_tabs.values():
            tab.rendered = False
            if self.views.tab(tab.frame, "text") == current:
                tab.render()
        # The BOM is read from the board FILE, so it goes stale on exactly the same
        # event -- a different board open, or the same one written again.
        self.bom_tab.rendered = False
        if current == bom_view.TAB_LABEL:
            self.bom_tab.render()

    def _pump(self) -> None:
        try:
            while True:
                self._events.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._pump)

    # ---------------------------------------------------------------- status

    # Segments joined by this, widest first.  Kept as one constant so the measuring code
    # and the rendering code cannot disagree about the separator's width.
    _STATUS_SEP = "   ·   "

    def _render_status_compact(self, width: int = 0) -> None:
        """Fit the mode-bar status line to the space it actually has.

        The line used to be one f-string in a label with `anchor="w"` and no wrapping, so
        when the mode bar ran out of room Tk simply cut the end off -- on a 1512 px window
        (the widest common MacBook) it read "Git waitin", clipped mid-word, and a state as
        ordinary as a model name plus a running cost needed 592 px in a 416 px slot. A
        status line that silently truncates is worse than a shorter one: the reader cannot
        tell a cut-off word from a real value.

        So drop whole segments from the least important end until it fits, and say so with
        a trailing ellipsis rather than pretending nothing was removed. Segment text is
        never itself chopped mid-word.
        """
        segments = list(getattr(self, "_status_segments", []))
        if not segments:
            return
        label = self.status_compact
        if not width:
            width = label.winfo_width()
        # Before the first map, winfo_width() is 1: show everything and let the <Configure>
        # binding re-render once a real width exists.
        if width <= 1:
            label.config(text=self._STATUS_SEP.join(reversed(segments)))
            return

        try:
            measure = tkfont.Font(font=label.cget("font")).measure
        except tk.TclError:                     # no font resolved yet; try again on resize
            label.config(text=self._STATUS_SEP.join(reversed(segments)))
            return

        kept = list(segments)
        while kept:
            text = self._STATUS_SEP.join(reversed(kept))
            if len(kept) < len(segments):
                text += " …"
            if measure(text) <= width or len(kept) == 1:
                label.config(text=text)
                return
            kept.pop(0)                          # drop the least important segment

    def _on_status_resize(self, event: "tk.Event") -> None:
        # Re-fit on the width Tk reports for the label itself, so this stays correct when
        # the window is resized, the chat pane is toggled, or a mode switch changes which
        # buttons share the bar.
        self._render_status_compact(event.width)

    def _update_status(self) -> None:
        sock = os.environ.get("KICAD_API_SOCKET", "") or "(default socket)"
        token = "token present" if os.environ.get("KICAD_API_TOKEN") else "no token"
        self.status_wiring.config(
            text=f"IPC {self.ipc_state}  ·  {sock}  ·  {token}  ·  "
                 f"PhiCad {self.kicad_version}")

        board = (self.ctx.board_name if self.ctx and self.ctx.has_board()
                 else "(no board open)")
        if self.bridge:
            ai = f"AI {self.bridge.display()}"
        elif self.backend:
            ai = f"AI {self.backend.display()}"
            # The model that actually answered, and what this conversation has cost,
            # both straight from the provider's own per-turn report (chat_backend.
            # TurnUsage).  A session whose backend reports nothing shows neither —
            # the sum is a floor of reported costs, never an estimate.
            usage = getattr(self.session, "last_usage", None)
            if usage and usage.model:
                ai += f" · {usage.model}"
            cost = getattr(self.session, "cost_usd_total", 0.0)
            if cost:
                ai += f" · ${cost:.4f}" if cost < 0.1 else f" · ${cost:.2f}"
        else:
            ai = ("AI UNAVAILABLE (no phi or shared session)" if host_bridge.AVAILABLE
                  else "AI UNAVAILABLE (no phi)")
        self.status_board.config(
            text=f"Board {board}  ·  {ai}  ·  {self.history_status}")

        # Agent mode hides the two-line block to give the render the window, so the same
        # facts have to survive somewhere: one line in the mode bar, still honest about a
        # dead socket or a missing backend rather than just going quiet.
        ai_short = ("session shared" if self.bridge else
                    "phi ready" if self.backend else "NO AI BACKEND")
        git_short = (self.history_status.split(" · ", 1)[0]
                     if self.history_status else "Git waiting")
        # Least to most important, so what survives a narrow window is what a person
        # needs first.  Board name and IPC state come last because they are the two
        # facts the whole window is about; git state goes first because it is the one
        # the status BLOCK also reports in manual mode.
        self._status_segments = [git_short, ai_short,
                                 f"IPC {self.ipc_state}", str(board)]
        self._render_status_compact()

        # The dot is a second channel for the same fact, never the only one: green is
        # only claimed when a board actually came back, because "connected" to an editor
        # with nothing open is not a state this window can answer questions in.
        if self.session is None or "not connected" in self.ipc_state \
                or "failed" in self.ipc_state or "error" in self.ipc_state:
            colour = ui_theme.LAYER_COPPER_F
        elif self.ctx and self.ctx.has_board():
            colour = ui_theme.LAYER_IN1
        else:
            colour = ui_theme.LAYER_SILK
        self.status_dot.itemconfigure(self._dot, fill=colour)

    def _probe_backend(self) -> None:
        chat_backend.probe_version(self.backend)
        self._post(self._update_status)

    # ------------------------------------------------------- context refresh

    def refresh_context(self) -> None:
        if self.busy_refresh:
            return
        self.busy_refresh = True
        self.refresh_btn.config(state=tk.DISABLED)
        self.refresh_state.config(text="reading board over IPC...")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            kicad = board_context.connect(timeout_ms=CONNECT_TIMEOUT_MS)
        except board_context.NotConnected as exc:
            # Bind the text NOW.  Python unbinds an `except ... as` name when the block
            # exits, and _post defers the lambda to the Tk loop, so closing over `exc`
            # raises NameError there -- which loses precisely the "cannot reach the IPC
            # API, here is how to fix it" message this branch exists to show.
            detail = str(exc)
            self._post(lambda: self._refresh_failed("not connected", detail, [
                "The window could not reach the KiCad IPC API.",
                "",
                "From inside PhiCad: Preferences > Plugins > enable the API server,",
                "then relaunch this window from the toolbar button.",
                "Standalone: start `phicad api-server <project>` and export",
                "KICAD_API_SOCKET before launching.",
            ]))
            return
        except Exception as exc:  # e.g. kipy not importable in this interpreter
            msg = f"{type(exc).__name__}: {exc}"
            self._post(lambda: self._refresh_failed("client error", msg, [
                "The IPC client could not start at all (kipy import/setup failure).",
                "Run via ai_view.sh so the interpreter is the repo's build/venv,",
                "or point PHICAD_AI_PYTHON at a python that has kipy installed.",
            ]))
            return

        try:
            version = kicad.get_version()
        except Exception:
            version = "?"

        try:
            ctx = board_context.collect_board_context(kicad)
        except board_context.NoBoardOpen as exc:
            # Not a dead end: launched from eeschema, this socket is the schematic
            # editor and can still name the project — render tabs, Git history and
            # the host bridge all key off the project on disk.  Only when the host
            # serves nothing at all does this stay a "no board" screen.
            try:
                ctx = board_context.collect_schematic_host_context(kicad)
            except Exception:
                ctx = None
            if ctx is None:
                reason = str(exc)

                def apply_no_board() -> None:
                    self.kicad_version = version
                    self.ipc_state = "connected"
                    self.ctx = None
                    self.digest = ("(no board is open in the editor - board facts "
                                   "are unavailable this turn)")
                    self.digest_dirty = True
                    self._clear_project_services()
                    self._set_context_pane(
                        "Connected to PhiCad, but no PCB is open.\n\n"
                        f"  {reason}\n\n"
                        "Open a board in the PCB editor and press Refresh.")
                    self._finish_refresh("no board open")
                self._post(apply_no_board)
                return
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"

            def apply_collect_error() -> None:
                self.kicad_version = version
                self.ipc_state = "connected (read failed)"
                self._refresh_failed("read failed", msg, [
                    "Connected, but reading the board raised the error above.",
                ])
            self._post(apply_collect_error)
            return

        def apply_ok() -> None:
            self.ctx = ctx
            self.kicad_version = ctx.kicad_version
            self.ipc_state = "connected"
            self.digest = ctx.ai_digest()
            self.digest_dirty = True
            self._set_context_pane(ctx.summary_text())
            self._finish_refresh(f"ok - read in {ctx.collect_seconds:.2f}s")
            # The board may have changed on disk (or be a different board entirely), so
            # every rendered view is now stale.  It may also be a project registered to
            # a shared host chat, which is discovered only after its path is known.
            self._invalidate_views()
            self._bind_project_services()
        self._post(apply_ok)

    def _refresh_failed(self, state: str, error: str, hints: list[str]) -> None:
        self.ipc_state = state
        self.ctx = None
        self.digest = None
        self.digest_dirty = False
        self._clear_project_services()
        self._set_context_pane("COULD NOT READ THE BOARD\n\n" + error + "\n\n"
                               + "\n".join(hints))
        # The success path invalidates the render panes; the failure path did not, so a
        # pane kept whatever it had drawn -- either a render of a board this window can no
        # longer reach, or an empty-state message written from the PREVIOUS ipc_state. That
        # is how the PCB tab came to say "IPC connecting..." while the mode bar already
        # said "client error". A pane that cannot be re-read must not keep asserting the
        # old answer.
        self._invalidate_views()
        self._finish_refresh(state)

    def _finish_refresh(self, note: str) -> None:
        self.busy_refresh = False
        self.refresh_btn.config(state=tk.NORMAL)
        # This label sits beside Refresh and says what the LAST refresh did -- "ok - read in
        # 0.42s", "no board open".  On failure the caller passes the ipc_state string, which
        # the status line already shows a few pixels to the left, so the bar read
        # "... IPC client error ... [client error] Refresh": the same fact twice, in two
        # different phrasings, which reads like two separate problems.  Say it once; the
        # status line owns connection state, this label owns the outcome of the last attempt.
        self.refresh_state.config(text="" if note == self.ipc_state else note)
        self._update_status()
        if self._autosend:
            msg, self._autosend = self._autosend, ""
            self.set_entry_text(msg)
            self.send_message()
        self._start_probe()

    def set_entry_text(self, text: str) -> None:
        """Replace the entry's contents, clearing the placeholder first.

        Both test hooks type through this: inserting over the placeholder would send the
        prompt text along with the message, which is a real bug the hooks would then
        have proven nothing about.
        """
        self._placeholder_on = False
        self.entry.delete("1.0", tk.END)
        self.entry.insert("1.0", text)
        self._autosize_entry()
        # Through the same call a keystroke makes, so a probe that types "/vie" proves
        # the completion popup, rather than proving a private helper.
        self._update_palette()

    def _set_context_pane(self, text: str) -> None:
        """Fill the Context tab, giving the digest's own structure some hierarchy.

        The digest is generated with rules and "Label : value" rows; tagging them is
        what turns eighty lines of monospace into something with an index.  Purely
        presentational -- the text handed to the model is ctx.ai_digest(), untouched.
        """
        self.context_text.config(state=tk.NORMAL)
        self.context_text.delete("1.0", tk.END)
        for line in text.splitlines():
            tag = ""
            if set(line.strip()) in ({"="}, {"-"}) and len(line.strip()) > 3:
                tag = "ctx_rule"
            elif line.startswith("--") or line.strip().startswith("=="):
                tag = "ctx_head"
            elif len(line) > 16 and line[:16].strip() and ":" in line[:20] \
                    and not line.startswith(" "):
                self.context_text.insert(tk.END, line[:17], "ctx_key")
                self.context_text.insert(tk.END, line[17:] + "\n")
                continue
            self.context_text.insert(tk.END, line + "\n", tag or ())
        self.context_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------------ chat

    def _on_return(self, event) -> str:
        self.send_message()
        return "break"

    def _say_system(self, text: str) -> None:
        self._say("System", text, "system")

    # The three families, with the sentence that says what each one is FOR.  Help that
    # lists fifteen commands without saying which ones touch the board is a reference,
    # not an answer.
    HELP_GROUPS = (
        ("Change the open board", board_edits.COMMANDS,
         "nothing reaches disk until /save, and Cmd-Z in the editor undoes the rest"),
        ("Move this window's view", view_commands.COMMANDS,
         "these never touch the board -- only what this window draws"),
        ("Design a new board", design_commands.COMMANDS,
         "describe a board in plain words first; the assistant answers with the IR"),
    )

    def _say_help(self, groups=None) -> None:
        """Every command the pane accepts, laid out to survive a narrow panel.

        Rendered here from the command tables rather than printed as the modules' aligned
        HELP text: two columns of monospace are 70 characters wide and the floating chat
        panel fits about 45, so the aligned table wrapped into nonsense in exactly the
        mode this window is meant to be used in.  Usage on one line, summary on the next,
        and it reads at any width.

        Handled here rather than in board_edits.run_command for a second reason: that
        path connects over IPC first, which made /help, of all things, fail when the
        editor was not reachable.  Help must work when nothing else does.
        """
        # /help is a slash command, so it runs even mid-turn; take the working line down
        # first for the same reason _say does.
        working = self._work_mark is not None
        if working:
            self._close_work_line()
        self.transcript.config(state=tk.NORMAL)
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert(tk.END, "\n")
        self.transcript.insert(tk.END, " Help ", "chip_system")
        self.transcript.insert(tk.END, f"  {time.strftime('%H:%M')}\n", "meta")
        self.transcript.insert(
            tk.END, "The assistant can recommend any of these; only you can run one. "
                    "A slash command is intercepted before the model ever sees it.\n",
            "body_dim")
        for title, commands, note in (groups or self.HELP_GROUPS):
            self.transcript.insert(tk.END, title + "\n", "heading")
            self.transcript.insert(tk.END, note + "\n", "body_dim")
            for name, args, summary in commands:
                # The trailing newline carries the `icode` tint too, which paints the
                # row to the full width -- deliberate: it makes the list read as rows.
                self.transcript.insert(tk.END, f"{name} {args}".strip() + "\n",
                                       ("body", "icode"))
                self.transcript.insert(tk.END, summary + "\n", "help_summary")
        self.transcript.config(state=tk.DISABLED)
        self.transcript.see(tk.END)
        if working:
            self._open_work_line()

    # ------------------------------------------------------- view / mode commands

    def focus_editor(self) -> None:
        """The Manual-mode button: put the PhiCad editor window back in front."""
        try:
            self._say("Editor", editor_control.raise_editor(), "view")
        except editor_control.EditorError as exc:
            self._say_error("Editor", str(exc))
        except Exception as exc:                                  # noqa: BLE001
            self._say_error("Editor", f"{type(exc).__name__}: {exc}")

    def _run_view_command(self, line: str) -> None:
        """Execute one /view, /mode, /chat or /editor command on the UI thread.

        These touch only this window (and, for /editor, the window manager), so they are
        instant and cannot block the Tk thread the way a render or a model turn would.
        """
        try:
            command = view_commands.parse(line)
        except view_commands.ViewError as exc:
            self._say_error("View", str(exc))
            return
        try:
            result = self._apply_view_command(command)
        except Exception as exc:                                  # noqa: BLE001
            self._say_error("View", f"{type(exc).__name__}: {exc}")
            return
        if result is view_commands.HELP:
            self._say_help([self.HELP_GROUPS[1]])
            return
        # /editor status is a key/value table and needs its alignment; /editor raise
        # answers in sentences, and monospacing those made a reply look like output.
        self._say("View", result, "view",
                  pre=command.verb == "editor" and command.arg == "status")

    def _apply_view_command(self, command: view_commands.ViewCommand) -> str:
        """Apply a parsed view command and report the state it ACTUALLY produced."""
        verb, arg = command.verb, command.arg

        if verb == "help":
            return view_commands.HELP
        if verb == "state":
            return self.describe_layout()
        if verb == "mode":
            return self.set_mode(arg)
        if verb == "chat":
            if arg == "toggle":
                return self.set_chat_open(not self.chat_open)
            return self.set_chat_open(arg == "open")
        if verb == "editor":
            return self._apply_editor_command(arg)
        if verb == "tab":
            return self._select_tab(arg)

        if verb in ("layers", "side"):
            key = "pcb" if verb == "layers" else "3d"
            tab = self.image_tabs[key]
            labels = [name for name, _ in tab.spec["modes"]]
            tab.mode.set(labels.index(arg))
            # Selecting the tab first means the render is sized for the pane it will be
            # drawn in, and it is also what the user meant by asking for that view.
            self._select_tab(key)
            tab.render()
            return (f"{self.views.tab(tab.frame, 'text')} tab set to {arg} and "
                    f"re-rendering.\n{self.describe_layout()}")

        key = self._current_image_key()
        if not key:
            return ("the Context tab is text, not a rendered image -- zoom and re-render "
                    "apply to the PCB, Schematic and 3D tabs.\n"
                    "Try /view pcb first.")
        tab = self.image_tabs[key]
        label = self.views.tab(tab.frame, "text")

        if verb == "render":
            tab.rendered = False
            tab.render()
            return f"re-rendering the {label} tab.\n{tab.describe()}"

        if verb == "zoom":
            if tab.pil_image is None:
                return (f"the {label} tab has no image to zoom yet: {tab.describe()}.\n"
                        "Run /view render, or wait for the render to finish.")
            if arg == "fit":
                tab.fit()
            elif arg == "in":
                tab.zoom_by(1.25)
            elif arg == "out":
                tab.zoom_by(1 / 1.25)
            else:
                tab.zoom_by(command.factor)
            # zoom_by clamps against the fit zoom, so the number asked for is not always
            # the number achieved -- report the one the canvas is actually drawing at.
            return f"{label} tab: {tab.describe()}"

        return f"unhandled view verb {verb!r} -- this is a bug in the pane."

    def _select_tab(self, key: str) -> str:
        widget = self.image_tabs[key].frame if key else self.ctx_tab
        self.views.select(widget)
        # <<NotebookTabChanged>> -- which is what starts a first render -- is delivered
        # from the event loop, so without this the report would say "not rendered yet"
        # about a tab whose render it just triggered.
        self.root.update_idletasks()
        label = self.views.tab(self.views.select(), "text")
        detail = self.image_tabs[key].describe() if key else "the live board digest"
        return f"showing the {label} tab: {detail}"

    def _apply_editor_command(self, verb: str) -> str:
        if verb == "raise":
            try:
                return editor_control.raise_editor()
            except editor_control.EditorError as exc:
                return str(exc)
        # status: probe the socket too, so "GUI editor" and "headless api-server" are
        # distinguished by an answer from the server rather than by a guess.
        kicad = None
        try:
            kicad = board_context.connect(timeout_ms=CONNECT_TIMEOUT_MS)
        except Exception:                                          # noqa: BLE001
            pass
        return editor_control.editor_status(kicad)

    def _run_edit_command(self, line: str) -> None:
        """Execute one /command against the open board, on the UI thread.

        Deliberately synchronous: these calls are single round-trips over a local socket,
        and doing them inline means the reply cannot interleave with a model turn.
        """
        try:
            kicad = board_context.connect(timeout_ms=CONNECT_TIMEOUT_MS)
            result = board_edits.run_command(kicad, kicad.get_board(), line)
        except board_edits.EditError as exc:
            self._say_error("Board", str(exc))
            return
        except board_context.NotConnected as exc:
            self._say_error("Board", f"not connected to a running PhiCad: {exc}")
            return
        except Exception as exc:                                  # noqa: BLE001
            self._say_error("Board", f"{type(exc).__name__}: {exc}")
            return

        # /help arrives here too (board_edits owns it), and its table needs its columns.
        self._say("Board", str(result), "board",
                  pre=line.strip().lower().startswith("/help"))

        # The board changed, so every rendered view and the context digest are now stale.
        # Invalidating rather than re-rendering keeps the edit fast; the next tab reveal
        # re-renders, which is the same lazy path a first reveal uses.
        self._invalidate_views()
        self.digest_dirty = True

    def _run_design_command(self, line: str) -> None:
        """Execute one /design or /synth command.

        `/synth build` takes tens of seconds — it opens an IPC session, imports a netlist,
        routes and runs DRC — so unlike a board edit it cannot run on the Tk thread without
        the window looking hung.  Everything else here is instant and stays inline.
        """
        if self.busy_design:
            self._say_error("Design", "a build is already running; wait for it to "
                                      "finish or fail.")
            return

        slow = line.strip().lower().startswith("/synth build")
        if not slow:
            try:
                result = design_commands.run_command(self.design, line, self.last_reply)
            except design_commands.DesignError as exc:
                self._say_error("Design", str(exc))
                return
            except Exception as exc:                                  # noqa: BLE001
                self._say_error("Design", f"{type(exc).__name__}: {exc}")
                return
            if result is design_commands.HELP:
                self._say_help([self.HELP_GROUPS[2]])
                return
            self._design_result(result, pre=line.strip().lower().startswith("/synth"))
            return

        self.busy_design = True
        self.send_btn.config(state=tk.DISABLED)
        self.chat_state.config(text="building")
        self._start_work("Compiling the design", "starting")
        self._begin_build_watch(line)

        def worker() -> None:
            try:
                out = design_commands.run_command(self.design, line, self.last_reply)
            except design_commands.DesignError as exc:
                out = str(exc)
            except Exception as exc:                                  # noqa: BLE001
                out = f"{type(exc).__name__}: {exc}"
            self._post(lambda: self._design_done(out))

        threading.Thread(target=worker, daemon=True).start()

    def _begin_build_watch(self, line: str) -> None:
        """Watch the output directory so a long build can report REAL progress.

        `--json` silences every stage line the compiler prints (phicad/synth/cli.py
        `_say`), so there is no progress stream to read; the only honest signal is the
        artefacts landing on disk.  This reports files it has seen appear -- never a
        stage it merely expects -- and the authoritative gate report is still the JSON
        the build returns at the end.
        """
        parts = line.strip().split()
        out_dir = parts[2] if len(parts) > 2 else None
        try:
            target = self.design.build_target(out_dir)
        except design_commands.DesignError:
            self._build_watch = None
            return
        self._build_watch = (target, target.name,
                             list(design_commands.BUILD_ARTEFACTS))
        self._poll_build()

    def _poll_build(self) -> None:
        if not self.busy_design or self._build_watch is None:
            return
        target, project, pending = self._build_watch
        for entry in list(pending):
            _, pattern, said = entry
            if (target / pattern.format(project=project)).exists():
                pending.remove(entry)
                self._work_note = said
        self.root.after(400, self._poll_build)

    def _design_done(self, result: str) -> None:
        self.busy_design = False
        self._build_watch = None
        elapsed = self._finish_work()
        self.send_btn.config(state=tk.NORMAL)
        self.chat_state.config(text="")
        self._design_result(result, pre=True, meta=f"{elapsed:.0f}s")

    def _design_result(self, result: str, pre: bool = False, meta: str = "") -> None:
        # `/design open` answers with a path rather than performing the open itself, so the
        # command module stays free of IPC and the GUI keeps the one connection.
        if result.startswith("OPEN:"):
            path = result[len("OPEN:"):]
            try:
                kicad = board_context.connect(timeout_ms=CONNECT_TIMEOUT_MS)
                from kipy.proto.common.commands.project_commands_pb2 import (
                    OpenDocument, OpenDocumentResponse)
                from kipy.proto.common.types import DocumentType
                kicad._client.send(
                    OpenDocument(type=DocumentType.DOCTYPE_PCB, path=path),
                    OpenDocumentResponse)
            except Exception as exc:                                  # noqa: BLE001
                self._say_error("Design",
                                f"could not open {path}: {type(exc).__name__}: {exc}")
                return
            self._say("Design", f"opened {path}", "design")
            self.refresh_context()
            return

        # A stage table whose first column is PASS/FAIL is a report of a failure too, so
        # the chip follows the content rather than the command: a build that stopped at a
        # gate reads as an error, which is what it is.
        kind = "error" if ("FAIL" in result or "error:" in result) else "design"
        self._say("Design", result, kind, meta=meta, pre=pre)

    def send_message(self) -> None:
        message = self.entry_text()
        if not message:
            return

        # A slash command is executed HERE and never sent to the model.  The model can
        # recommend a command but cannot run one: the user has to type it, so
        # human-in-the-loop is structural rather than a rule the model might not follow.
        #
        # ORDER IS LOAD-BEARING: board_edits.is_command() returns True for ANY line that
        # starts with "/", so every more specific family has to be tested before it.
        is_slash = (message.strip().lower() in ("/help", "/?")
                    or design_commands.is_command(message)
                    or view_commands.is_command(message)
                    or board_edits.is_command(message))

        # A turn in flight used to swallow the next line silently -- the text stayed in
        # the box, nothing happened, and nothing said why.  Slash commands are local and
        # run regardless; only a second question has to wait, and now it says so and
        # keeps what was typed.
        if self.busy_chat and not is_slash:
            self._say_system("still waiting on the current answer. Your message is "
                             "still in the box; press Send again when it lands, or "
                             "press Stop.")
            return

        self.entry.delete("1.0", tk.END)
        self._hide_palette()
        # Only when the box is not focused: the placeholder is real text, and putting it
        # back under a live cursor would make the next keystroke land inside it.
        if self.root.focus_get() is not self.entry:
            self._show_placeholder()
        self._autosize_entry()
        self._set_hint()

        context = self.digest if (self.digest and self.digest_dirty) else None
        self._say("You", message, "user")

        if message.strip().lower() in ("/help", "/?"):
            self._say_help()
            return

        if design_commands.is_command(message):
            self._run_design_command(message)
            return

        if view_commands.is_command(message):
            self._run_view_command(message)
            return

        if board_edits.is_command(message):
            self._run_edit_command(message)
            return

        if self.session is None:
            would = chat_backend.ChatSession.compose(message, context or self.digest)
            self._say("System",
                      "No AI backend - NOT sent. This is exactly what would have "
                      "been submitted:\n\n```\n" + would + "\n```", "system")
            return

        if context:
            self.digest_dirty = False

        self.busy_chat = True
        self._cancelled = False
        self.send_btn.config(state=tk.DISABLED)
        self.chat_state.config(text="thinking")
        self._start_work("Thinking",
                         "grounded with the live board context" if context else "",
                         stoppable=True)

        threading.Thread(target=self._chat_worker, args=(message, context),
                         daemon=True).start()

    def _chat_worker(self, message: str, context: str | None) -> None:
        def started(proc) -> None:
            self._post(lambda: setattr(self, "_chat_proc", proc))

        try:
            reply = self.session.ask(message, context, on_start=started)
            self._post(lambda: self._chat_done("AI", "ai", reply))
        except (chat_backend.ChatError, host_bridge.BridgeError) as exc:
            # Same trap as _refresh_worker: the name is gone by the time Tk runs this.
            detail = str(exc)
            self._post(lambda: self._chat_done("Error", "error", detail))
        except Exception as exc:
            msg = f"unexpected failure: {type(exc).__name__}: {exc}"
            self._post(lambda: self._chat_done("Error", "error", msg))

    def _chat_done(self, who: str, kind: str, body: str) -> None:
        # Kept so /design save has something to read the design out of.  Only a real model
        # answer counts: saving from an error message would write nonsense to disk.
        if kind == "ai":
            self.last_reply = body
        elapsed = self._finish_work()
        self._chat_proc = None
        if self._cancelled and kind == "error":
            who, body = "Stopped", "you stopped the turn. Nothing was sent on."
        self._say(who, body, kind, meta=f"{elapsed:.0f}s")
        self.busy_chat = False
        self.send_btn.config(state=tk.NORMAL)
        turns = self.session.turns if self.session else 0
        self.chat_state.config(text=f"{turns} turn{'s' if turns != 1 else ''}"
                               if turns else "")
        # The status line owns the model/cost readout; refresh it now that the
        # turn's provider-reported usage (if any) has landed on the session.
        self._update_status()
        # An answer that lands while the floating panel is shut would otherwise be
        # invisible.  Nudge rather than force: opening the panel over the render on the
        # user's behalf is exactly the kind of thing agent view exists to avoid.
        if self.mode == ui_state.MODE_AGENT and not self.chat_open:
            self.chat_open_btn.config(text="Chat *")

    def _on_close(self) -> None:
        self._persist()
        # Capture changes that landed inside the debounce window.  This stages only the
        # board-project scope, never unrelated files in an enclosing monorepo or PhiCad tree.
        if self.history is not None:
            try:
                self.history.snapshot("phicad: close Agent View")
            except project_history.HistoryError as exc:
                print(f"ai-view: final Git snapshot failed: {exc}", file=sys.stderr)
        self.root.destroy()

    # -------------------------------------------------------- layout verification

    def layout_snapshot(self, label: str = "") -> dict:
        """Measure the layout, so a claim about it can be an assertion rather than a look.

        Used by PHICAD_AI_PROBE.  Everything in here comes from Tk itself -- winfo_*, the
        PanedWindow's own pane list, the notebook's style -- never from what the code
        believes it did.
        """
        self.root.update_idletasks()

        def geometry(widget) -> dict:
            # reqw is what the widget ASKED for.  A control clipped by a short cavity
            # still reports mapped and still has a width; reqw > w is the only way to
            # see that it was cut, and cut controls are what a previous pass shipped.
            return {"mapped": bool(widget.winfo_ismapped()),
                    "manager": widget.winfo_manager(),
                    "w": widget.winfo_width(), "h": widget.winfo_height(),
                    "reqw": widget.winfo_reqwidth(), "reqh": widget.winfo_reqheight(),
                    "x": widget.winfo_rootx(), "y": widget.winfo_rooty()}

        try:
            tab = self.views.tab(self.views.select(), "text")
        except tk.TclError:
            tab = ""
        try:
            root_state = self.root.state()
        except tk.TclError:
            root_state = "?"

        # The first line of the context pane, because the degradation promise ("never
        # fabricate, always explain") is only testable if the explanation is measurable.
        # A NameError in a deferred error lambda used to leave this blank.
        context_head = self.context_text.get("1.0", "2.0").strip()

        return {
            "after": label,
            "mode": self.mode,
            "chat_open": self.chat_open,
            "tab": tab,
            "context_head": context_head,
            "ipc_state": self.ipc_state,
            # What the pane answered the last command with, so a probe can assert on the
            # reply and not merely on the geometry it produced.
            "reply_tail": self.transcript.get("1.0", tk.END).strip()[-700:],
            "root_geometry": self.root.geometry(),
            "root_state": root_state,
            "notebook_style": str(self.views.cget("style")),
            "panes": [str(pane) for pane in self.panes.panes()],
            "state_file": ui_state.state_path(),
            # The entry row is in here because it is what a previous pass got wrong
            # twice: the Send button was never mapped, and the entry was squeezed to
            # zero height.  Both are invisible in a screenshot and obvious in winfo.
            "widgets": {name: geometry(widget) for name, widget in (
                ("modebar", self.modebar),
                ("statusbar", self.statusbar),
                ("viewhead", self.viewhead),
                ("views", self.views),
                ("chat", self.chat),
                ("chat_open_btn", self.chat_open_btn),
                ("editor_btn", self.editor_btn),
                ("viewsel", self.viewsel_frame),
                ("transcript", self.transcript),
                ("entry", self.entry),
                ("send_btn", self.send_btn),
                ("hint", self.hint),
                ("palette", self.palette),
                # The mode bar's own children.  Without these, a `modebar` reqw wider
                # than the window says only THAT it overflows, not which control is
                # responsible -- and estimating button widths from font metrics gets it
                # wrong (aqua's real padding is not the padding you assume).
                ("modebar_status", self.status_compact),
                ("modebar_right", self.refresh_btn.master),
                ("modebar_refresh", self.refresh_btn),
                ("modebar_action", self.mode_action),
            )},
            # The visible tab's own toolbar: a control clipped to nothing still answers
            # winfo_ismapped, so width is what the assertion has to be about.
            "view_controls": {
                name: geometry(widget)
                for key, tab in self.image_tabs.items()
                if key == self._current_image_key()
                for name, widget in (("reload_btn", tab.reload_btn),
                                     ("controls", tab.controls),
                                     ("canvas", tab.canvas))
            },
            # The BOM tab's own measurements.  Its table and its detail pane compete for
            # one cavity, so "the table has rows" and "the inspector is still on screen"
            # are two different claims and a screenshot proves only the first.
            "bom": {
                "rows": len(self.bom_tab.tree.get_children()),
                "grouped": bool(self.bom_tab.grouped.get()),
                "parts": len(self.bom_tab.parts),
                "summary": self.bom_tab.summary.cget("text"),
                "describe": self.bom_tab.describe(),
                "widgets": {name: geometry(widget) for name, widget in (
                    ("tree", self.bom_tab.tree),
                    ("summary", self.bom_tab.summary),
                    ("detail", self.bom_tab.detail),
                    ("preview", self.bom_tab.preview),
                    ("fields", self.bom_tab.fields),
                    ("fetch_btn", self.bom_tab.fetch_btn),
                    ("controls", self.bom_tab.controls),
                )},
            },
            "palette_open": self.palette_open,
            "palette_items": [name for name, _, _ in self.palette_items],
            "entry_text": self.entry.get("1.0", "end-1c"),
            "placeholder_on": bool(getattr(self, "_placeholder_on", False)),
            "entry_height_lines": int(self.entry.cget("height")),
            "code_columns": self._code_width(),
        }

    def _start_probe(self) -> None:
        if not self._probe or self._probe_started:
            return
        self._probe_started = True
        self.root.after(self._probe_ms, self._probe_step)

    def _probe_step(self) -> None:
        self._probe_log.append(self.layout_snapshot(self._probe_last))
        if not self._probe:
            self._probe_finish()
            return
        line = self._probe.pop(0)
        self._probe_last = line
        # Through send_message, not the handler directly: the probe has to exercise the
        # same dispatch a typed line does, or it proves nothing about the real path.
        # "type:" leaves the text in the box unsent, which is the only way to snapshot a
        # state that exists only WHILE typing -- the completion popup.
        if line.startswith("type:"):
            self.entry.focus_set()
            # "\n" spelled out, because the probe list is split on ";" and cannot carry a
            # real newline.  This is how the multi-line paste case gets tested at all.
            self.set_entry_text(line[len("type:"):].replace("\\n", "\n"))
        else:
            self.set_entry_text(line)
            self.send_message()
        self.root.after(self._probe_ms, self._probe_step)

    def _probe_finish(self) -> None:
        blob = json.dumps(self._probe_log, indent=2)
        out = os.environ.get("PHICAD_AI_PROBE_OUT", "").strip()
        if out:
            try:
                with open(out, "w", encoding="utf-8") as handle:
                    handle.write(blob + "\n")
            except OSError as exc:
                print(f"ai-view: could not write {out}: {exc}", file=sys.stderr)
                print(blob)
        else:
            print(blob)
        if os.environ.get("PHICAD_AI_PROBE_HOLD", "").strip() not in ("1", "true", "yes"):
            self.root.destroy()
            return
        # Held open to be photographed.  Forced to the top because the whole point of a
        # screenshot is that it shows THIS window, and a KiCad editor sitting in front of
        # it produces a picture that proves nothing.
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            pass


def main() -> int:
    root = tk.Tk()
    AiViewApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
