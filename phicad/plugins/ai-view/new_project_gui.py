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
"""New AI Project: describe a board in plain words, get a routed project, opened in PhiCad.

Launched from the project manager's launcher tile (scope "project_manager" in plugin.json,
through new_project.sh).  The dashboard injects KICAD_API_SOCKET / KICAD_API_TOKEN, and the
manager serves OpenDocument (kicad/kicad.cpp), so the finished project is opened over IPC
in the very window the user started from.

THE CONTRACT (same safety boundary as the AI View pane's /design flow):

    phi PROPOSES  ->  synth CHECKS  ->  phi repairs machine feedback automatically
    ->  repeat until valid  ->  the USER approves  ->  synth builds and gates the board

The model never writes a project and never starts a build.  Validation may revise an
in-memory proposal, but "Approve design" remains the human boundary before synthesis.
`design_agent.py` owns the provider-neutral model/repair loop and emits versioned events;
`design_commands.py` owns the compiler process.  The Tk window is only one host adapter.

Visual language follows ai_view_gui.py (same fonts, same who_user/who_ai ink colours, the
accent orange accent from make_icons.py) so the fork's windows read as one family.

THREADING: tkinter is single-threaded; phi calls and builds run on worker threads that
post closures to a queue the Tk loop drains every 60 ms.  Workers never touch widgets.

DEGRADATION: without a `phi` CLI the chat refuses honestly and says how to fix it.  A
pasted design still runs the machine check and can be approved if valid; an invalid one
reports that automatic repair needs a model adapter instead of asking the user to relay
its diagnostics manually.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

import chat_backend
import design_agent
import design_commands
import project_history
import ui_theme
from design_commands import DesignError, DesignSession, extract_design, format_check_report
from pathlib import Path

APP_TITLE = "New Project — PhiCad"
ORANGE = "#f7941d"            # accent orange, same as make_icons.py
DEFAULT_PROJECT_PARENT = os.path.join(os.path.expanduser("~"), "Documents", "PhiCad", "Projects")

# How many times a FAILED BUILD may be sent back for automatic repair.  Preflight repairs are
# bounded separately inside design_agent; this bounds the far more expensive outer cycle, where
# every round runs a full place/route/DRC pass.
MAX_BUILD_REPAIRS = int(os.environ.get("PHICAD_AI_BUILD_REPAIRS", "3"))

# Gate names printed by `phicad synth build`, longest-lived first: the failing one is the only
# part of a 20-line log that belongs in the model's `where` field.
_GATE_LINE = re.compile(r"^\s*FAIL\s+(\S+)", re.MULTILINE)


def _failed_gate(build_log: str) -> str:
    """The name of the gate that failed, e.g. 'route', or '' when the log does not say."""
    matches = _GATE_LINE.findall(build_log or "")
    return matches[-1] if matches else ""

SYSTEM_PROMPT = """\
You are the PhiCad new-project assistant, embedded in a fork of KiCad.  The user
wants a printed circuit board that does not exist yet.  Your job is to firm up the intent
in a SHORT conversation and then answer with a design the compiler can build.

Conversation rules:
- Do not demand a spec up front.  If the request is missing something the design needs,
  ask for it -- but ask for at most two or three things at a time, offer a sensible
  default for each, and prefer assuming a reasonable default over interrogating.  The
  usual gaps: supply voltage and where power comes from, the host interface (I2C, SPI,
  UART, USB), and what connectors the outside world plugs into.
- When the intent is clear enough, reply with one or two sentences on what the circuit
  does and what you assumed, then ONE JSON object in a single fenced ```json block --
  the design IR -- and nothing else in that block.
- PhiCad validates every proposal automatically.  If a gate fails, PhiCad sends you its
  structured diagnostics in an automatic repair turn; answer with a corrected COMPLETE
  design in a new ```json block.  Never ask the user to copy or paste gate output.
- The user reviews and approves only after the design passes validation, then starts the
  build.  YOU cannot save, build, or open anything, and must never claim you did.

The design IR (only the keys you need):

```json
{
  "project": "sensor-node",
  "title": "I2C sensor node",
  "board": {"layers": 2, "outline": "auto", "margin": 2.0,
            "corner": {"chamfer": 2.0}, "ground_net": "GND", "pour_ground": true},
  "rules": {"clearance": 0.2, "track_width": 0.25,
            "track_widths": {"3V3": 0.4, "GND": 0.4}},
  "parts": {
    "J1": {"footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
           "value": "I2C+PWR"},
    "U1": {"footprint": "Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering",
           "value": "BME280", "lcsc": "C92489"}
  },
  "nets": {"GND": ["J1.1", "U1.1", "U1.7"], "3V3": ["J1.2", "U1.6", "U1.8"]},
  "no_connect": [],
  "placement": {"J1": {"edge": "west"}, "C1": {"near": "U1", "within": 2.5}}
}
```

Rules for the design, all of them things the compiler will otherwise reject:
- EVERY pad of every part must appear in exactly one net or in "no_connect".  A pad you
  forgot is an error, not a warning.  Count the footprint's pads before writing the nets.
- Pins are written "REF.PAD" using the FOOTPRINT's pad names, which are not always the
  chip's pin names.  If unsure what pads a footprint has, say so and ask rather than
  guessing -- a wrong pad number fails the gate with the real list in the message.
- Footprints are "Library:Name" from the stock KiCad libraries.  Use ones you are sure
  exist; a wrong name fails with the closest real matches listed.
- A package-generic footprint (SOT-23, SOIC-8, LGA...) says nothing about the part on
  it, so give those parts a "symbol" ("Library:Name" from the stock symbol libraries).
- You do not choose coordinates.  Express intent -- "near", "within", "edge", "group" --
  and let the placer resolve it.  Only use "at": [x, y] for something fixed by the
  outside world, like a connector aligned to an enclosure cutout.
- Give parts real values and, where you know them, mpn/lcsc.
- Keep it to two copper layers unless the user insists; the router only uses two."""


class NewProjectWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("860x720")
        self.root.minsize(680, 560)

        self._events: "queue.Queue[object]" = queue.Queue()
        self.busy = False
        self.validating = False
        self.validated = False
        self.building = False
        self.design: dict | None = None
        self.accepted = False
        self.validated_digest: str | None = None
        self.approved_digest: str | None = None
        self.built_pro: str | None = None
        self.last_reply = ""
        # The synth-check loop is bounded inside the workflow, but a design that passes
        # every preflight and then fails a build gate re-enters it from here.  Without its
        # own bound that outer cycle can repeat forever on a design the model cannot fix,
        # each round costing a full build; and a silent retry looks identical to a hang.
        self.build_repairs = 0
        self.max_build_repairs = MAX_BUILD_REPAIRS

        self.session = DesignSession()
        self.backend = chat_backend.resolve_backend()
        self.model: design_agent.DesignModel | None = (
            design_agent.PhiCliDesignModel(
                self.backend, SYSTEM_PROMPT,
                # Backends with structured output (codex) get the design-IR
                # schema bound to every reply; others rely on the repair loop.
                schema_file=design_agent.design_ir_schema_file(self.backend))
            if self.backend is not None else None
        )
        self.validator = design_agent.DesignValidationWorkflow(self.session, self.model)

        self._build_ui()
        self._greet()
        self.root.after(60, self._pump)
        if self.backend:
            threading.Thread(target=self._probe_backend, daemon=True).start()

    def _probe_backend(self) -> None:
        """Resolve the phi version without holding an empty window on startup."""
        chat_backend.probe_version(self.backend)
        self._post(self._restore_backend_status)

    def _restore_backend_status(self) -> None:
        if self.backend is None:
            self.status.config(text="phi unavailable", foreground=ui_theme.ON_LIGHT_RED)
            return
        version = self.backend.version
        self.status.config(text=f"phi {version}" if version else "phi ready",
                           foreground=ui_theme.TEXT_DIM)

    # ------------------------------------------------------------------ UI construction

    def _build_ui(self) -> None:
        # Match the shell Tk actually draws.  The old window assumed a white surface;
        # on macOS dark mode its default-black text disappeared and the expanding
        # transcript also squeezed the composer out of the window.
        ui_theme.use_system_appearance(self.root)
        self.fonts = ui_theme.Fonts.resolve(self.root)
        self.hero_font = (self.fonts.title[0], 20, "bold")
        ui_theme.apply_ttk_styles(ttk.Style(self.root), self.fonts)

        self.root.configure(background=ui_theme.SURFACE)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

        # Header: product cue and task on the left, one compact health signal on the
        # right.  Everything else belongs in the conversation or the workflow card.
        header = tk.Frame(self.root, background=ui_theme.SURFACE)
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 2))
        header.grid_columnconfigure(0, weight=1)

        heading = tk.Frame(header, background=ui_theme.SURFACE)
        heading.grid(row=0, column=0, sticky="w")
        tk.Label(heading, text="NEW AI PROJECT", font=self.fonts.small_bold,
                 foreground=ORANGE, background=ui_theme.SURFACE).pack(anchor="w")
        tk.Label(heading, text="What are you building?", font=self.hero_font,
                 foreground=ui_theme.TEXT, background=ui_theme.SURFACE).pack(anchor="w")

        backend = tk.Frame(header, background=ui_theme.SURFACE)
        backend.grid(row=0, column=1, sticky="e")
        dot = tk.Canvas(backend, width=9, height=9, highlightthickness=0,
                        borderwidth=0, background=ui_theme.SURFACE)
        dot.pack(side=tk.LEFT, padx=(0, 7))
        dot.create_oval(1, 1, 8, 8, outline="",
                        fill=(ui_theme.ON_LIGHT_GREEN if self.backend
                              else ui_theme.ON_LIGHT_RED))
        backend_txt = "phi ready" if self.backend else "phi unavailable"
        self.status = tk.Label(
            backend, text=backend_txt, font=self.fonts.small,
            foreground=(ui_theme.TEXT_DIM if self.backend else ui_theme.ON_LIGHT_RED),
            background=ui_theme.SURFACE,
        )
        self.status.pack(side=tk.LEFT)

        sub = ("Describe the board in plain words. PhiCad checks and repairs the proposal "
               "automatically; you approve only the validated design before it builds.")
        tk.Label(self.root, text=sub, font=self.fonts.ui,
                 foreground=ui_theme.TEXT_DIM, background=ui_theme.SURFACE,
                 wraplength=820, justify=tk.LEFT, anchor="w").grid(
                     row=1, column=0, sticky="ew", padx=18, pady=(2, 12))

        # Transcript.  Grid gives this the only expanding row, so fixed controls below
        # remain visible regardless of the window height.
        body = tk.Frame(self.root, background=ui_theme.LINE, borderwidth=0)
        body.grid(row=2, column=0, sticky="nsew", padx=18)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.text = tk.Text(
            body, wrap=tk.WORD, font=self.fonts.ui, state=tk.DISABLED,
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
            background=ui_theme.SURFACE_SUB, foreground=ui_theme.TEXT,
            selectbackground=ui_theme.ON_LIGHT_BLUE, selectforeground="#ffffff",
            padx=14, pady=12, spacing1=1, spacing3=3,
        )
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        scroll.grid(row=0, column=1, sticky="ns", pady=1)
        for tag, cfg in {
            "who_user": {"font": self.fonts.small_bold,
                         "background": ui_theme.ON_LIGHT_BLUE, "foreground": "#ffffff",
                         "spacing1": 9, "spacing3": 3},
            "who_ai": {"font": self.fonts.small_bold,
                       "background": ui_theme.ON_LIGHT_MAGENTA,
                       "foreground": ui_theme.INK, "spacing1": 9, "spacing3": 3},
            "who_sys": {"font": self.fonts.small_bold,
                        "background": ui_theme.SURFACE_SUNK,
                        "foreground": ui_theme.TEXT_DIM, "spacing1": 9, "spacing3": 3},
            "msg": {"lmargin1": 14, "lmargin2": 14, "rmargin": 10},
            "sys": {"lmargin1": 14, "lmargin2": 14, "rmargin": 10,
                    "foreground": ui_theme.TEXT_DIM},
            "err": {"lmargin1": 14, "lmargin2": 14, "rmargin": 10,
                    "foreground": ui_theme.ON_LIGHT_RED},
            "mono": {"lmargin1": 14, "lmargin2": 28, "rmargin": 8,
                     "font": self.fonts.mono_small, "background": ui_theme.CODE_BG,
                     "foreground": ui_theme.CODE_FG},
            "pending": {"lmargin1": 14, "lmargin2": 14,
                        "foreground": ui_theme.TEXT_FAINT},
        }.items():
            self.text.tag_configure(tag, **cfg)

        # Composer: label and keyboard hint make the input's purpose discoverable, and
        # a fixed grid row keeps it present while the transcript absorbs resize pressure.
        composer = tk.Frame(self.root, background=ui_theme.SURFACE)
        composer.grid(row=3, column=0, sticky="ew", padx=18, pady=(10, 8))
        composer.grid_columnconfigure(0, weight=1)
        label_row = tk.Frame(composer, background=ui_theme.SURFACE)
        label_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        tk.Label(label_row, text="Describe your board", font=self.fonts.small_bold,
                 foreground=ui_theme.TEXT, background=ui_theme.SURFACE).pack(side=tk.LEFT)
        tk.Label(label_row, text="Return to send  ·  Shift+Return for a new line",
                 font=self.fonts.small, foreground=ui_theme.TEXT_FAINT,
                 background=ui_theme.SURFACE).pack(side=tk.RIGHT)

        entry_box = tk.Frame(composer, background=ui_theme.LINE)
        entry_box.grid(row=1, column=0, sticky="ew")
        self.entry = tk.Text(
            entry_box, height=2, wrap=tk.WORD, font=self.fonts.ui, relief=tk.FLAT,
            borderwidth=0, highlightthickness=0, background=ui_theme.SURFACE_SUB,
            foreground=ui_theme.TEXT, insertbackground=ui_theme.ON_LIGHT_BLUE,
            selectbackground=ui_theme.ON_LIGHT_BLUE, selectforeground="#ffffff",
            padx=10, pady=8,
        )
        self.entry.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.send_btn = ttk.Button(composer, text="Send", command=self.send_message)
        self.send_btn.grid(row=1, column=1, sticky="ns", padx=(8, 0), ipadx=10)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.entry.focus_set()

        # Design workflow card: hidden until a proposal exists.  Its own state gets a
        # full row rather than competing with four buttons for the remaining width.
        self.card = tk.Frame(self.root, background=ui_theme.SURFACE_SUB,
                             highlightthickness=1, highlightbackground=ui_theme.LINE)
        card_head = tk.Frame(self.card, background=ui_theme.SURFACE_SUB)
        card_head.pack(fill=tk.X, padx=12, pady=(10, 4))
        self.card_title = tk.Label(card_head, text="", font=self.fonts.ui_bold,
                                   background=ui_theme.SURFACE_SUB,
                                   foreground=ui_theme.TEXT, anchor="w")
        self.card_title.pack(fill=tk.X)
        self.card_state = tk.Label(card_head, text="", font=self.fonts.small,
                                   background=ui_theme.SURFACE_SUB,
                                   foreground=ui_theme.TEXT_DIM, anchor="w")
        self.card_state.pack(fill=tk.X, pady=(2, 0))

        dest_row = tk.Frame(self.card, background=ui_theme.SURFACE_SUB)
        dest_row.pack(fill=tk.X, padx=12, pady=(3, 6))
        tk.Label(dest_row, text="Create in", font=self.fonts.small,
                 background=ui_theme.SURFACE_SUB,
                 foreground=ui_theme.TEXT_DIM).pack(side=tk.LEFT)
        self.dest = tk.Entry(
            dest_row, font=self.fonts.small, relief=tk.FLAT, borderwidth=0,
            highlightthickness=1, highlightbackground=ui_theme.LINE,
            highlightcolor=ui_theme.ON_LIGHT_BLUE, background=ui_theme.SURFACE,
            foreground=ui_theme.TEXT, insertbackground=ui_theme.ON_LIGHT_BLUE,
        )
        self.dest.insert(0, DEFAULT_PROJECT_PARENT)
        self.dest.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 6), ipady=4)
        ttk.Button(dest_row, text="Browse…", command=self._browse,
                   style="Small.TButton").pack(side=tk.RIGHT)

        btn_row = tk.Frame(self.card, background=ui_theme.SURFACE_SUB)
        btn_row.pack(fill=tk.X, padx=12, pady=(0, 10))
        self.review_btn = ttk.Button(btn_row, text="Review JSON", command=self._review)
        self.review_btn.pack(side=tk.LEFT)
        self.accept_btn = ttk.Button(btn_row, text="Approve design", command=self._accept,
                                     state=tk.DISABLED)
        self.accept_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.build_btn = ttk.Button(btn_row, text="Build project", command=self._build,
                                    state=tk.DISABLED)
        self.build_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.open_btn = ttk.Button(btn_row, text="Open in PhiCad", command=self._open,
                                   state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Build progress: hidden until a build starts.
        self.progress = tk.Text(
            self.root, height=6, font=self.fonts.mono_small, state=tk.DISABLED,
            relief=tk.FLAT, borderwidth=0, highlightthickness=1,
            highlightbackground=ui_theme.LINE, background=ui_theme.CODE_BG,
            foreground=ui_theme.CODE_FG, padx=10, pady=7,
        )
        self.progress.tag_configure("pass", foreground=ui_theme.ON_LIGHT_GREEN)
        self.progress.tag_configure("fail", foreground=ui_theme.ON_LIGHT_RED)
        self.progress.tag_configure("accent", foreground=ORANGE)

    # ------------------------------------------------------------------ transcript

    def _append(self, who: str, who_tag: str, body: str, tag: str = "msg") -> None:
        self.text.config(state=tk.NORMAL)
        label = who or ("Error" if tag == "err" else "Info")
        self.text.insert(tk.END, f" {label} ", who_tag)
        self.text.insert(tk.END, "\n")
        self.text.insert(tk.END, body.rstrip() + "\n\n", tag)
        self.text.config(state=tk.DISABLED)
        self.text.see(tk.END)

    def _append_reply(self, body: str) -> None:
        """AI reply, with fenced blocks rendered monospace so the design reads as code."""
        self.text.config(state=tk.NORMAL)
        self.text.insert(tk.END, " phi ", "who_ai")
        self.text.insert(tk.END, "\n")
        pos = 0
        for match in re.finditer(r"```[a-zA-Z0-9]*\s*\n(.*?)```", body, re.DOTALL):
            before = body[pos:match.start()].strip()
            if before:
                self.text.insert(tk.END, before + "\n", "msg")
            self.text.insert(tk.END, match.group(1).rstrip() + "\n", "mono")
            pos = match.end()
        tail = body[pos:].strip()
        if tail:
            self.text.insert(tk.END, tail + "\n", "msg")
        self.text.insert(tk.END, "\n")
        self.text.config(state=tk.DISABLED)
        self.text.see(tk.END)

    def _greet(self) -> None:
        if self.backend:
            self._append("", "who_sys",
                         "Say what you want to build — “a USB-C powered BME280 sensor "
                         "node with I2C broken out to a 4-pin header”. phi will ask about "
                         "anything it genuinely needs, then PhiCad will check and repair "
                         "the proposal automatically before asking you to approve it.", "sys")
        else:
            self._append("", "who_sys",
                         "The phi CLI was not found, so chat is disabled. Install "
                         "phi and sign in; the harness is not configurable. You can still "
                         "paste a design JSON — an "
                         "object with \"parts\" and \"nets\" — into the box below and "
                         "build it.", "err")

    # ------------------------------------------------------------------ chat flow

    def _on_return(self, event) -> str | None:
        if event.state & 0x0001:  # Shift held: newline
            return None
        self.send_message()
        return "break"

    def send_message(self) -> None:
        if self.busy or self.building:
            return
        message = self.entry.get("1.0", tk.END).strip()
        if not message:
            return
        self.entry.delete("1.0", tk.END)
        self._append("You", "who_user", message, "msg")

        # A pasted design counts as a proposal the user can accept -- with or without phi.
        pasted = extract_design(message)
        if pasted is not None:
            self._design_found(pasted, source="pasted")
            return

        if self.backend is None:
            if pasted is None:
                self._append("", "who_sys",
                             "phi is not available; only a pasted design JSON works here.",
                             "err")
            return

        self.busy = True
        self.send_btn.config(state=tk.DISABLED)
        self.status.config(text="phi is thinking…", foreground=ui_theme.TEXT_DIM)

        def worker() -> None:
            try:
                reply = self._ask_phi(message)
                self._post(lambda: self._chat_done(reply, None))
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                self._post(lambda: self._chat_done(None, err))

        threading.Thread(target=worker, daemon=True).start()

    def _ask_phi(self, message: str) -> str:
        """One provider-neutral design turn through the configured model adapter."""
        if self.model is None:
            raise chat_backend.ChatError("no design model adapter is configured")
        return self.model.ask(message)

    def _chat_done(self, reply: str | None, error: str | None) -> None:
        self.busy = False
        self.send_btn.config(state=tk.NORMAL)
        self._restore_backend_status()
        if error is not None:
            self._append("", "who_sys", error, "err")
            return
        self.last_reply = reply or ""
        self._append_reply(self.last_reply)
        design = extract_design(self.last_reply)
        if design is not None:
            self._design_found(design, source="proposed")

    # ------------------------------------------------------------------ design card

    def _design_found(self, design: dict, source: str) -> None:
        self.design = dict(design)
        self.validated = False
        self.accepted = False
        self.validated_digest = None
        self.approved_digest = None
        self.built_pro = None
        self.accept_btn.config(text="Validating…", state=tk.DISABLED)
        self.build_btn.config(state=tk.DISABLED)
        self.open_btn.config(state=tk.DISABLED)
        self._set_design_summary(f"Design {source}")
        self.card_state.config(text="Running the synth checker before you approve anything…",
                               foreground=ui_theme.TEXT_DIM)
        if not self.card.winfo_manager():
            self.card.grid(row=4, column=0, sticky="ew", padx=18, pady=(2, 8))
        self._start_validation()

    def _set_design_summary(self, prefix: str) -> None:
        if self.design is None:
            return
        name = self.design.get("project", "design")
        parts = len(self.design.get("parts", {}))
        nets = len(self.design.get("nets", {}))
        part_word = "part" if parts == 1 else "parts"
        net_word = "net" if nets == 1 else "nets"
        title = f"{prefix}: {name} — {parts} {part_word}, {nets} {net_word}"
        self.card_title.config(text=title)

    def _review(self) -> None:
        if self.design is None:
            return
        self._append("Design", "who_sys", json.dumps(self.design, indent=2), "mono")

    def _start_validation(self, initial_feedback: dict | None = None) -> None:
        """Check and repair on a worker; only a validated design can be approved."""
        if self.design is None or self.validating or self.building:
            return
        self.validating = True
        self.validated = False
        self.accepted = False
        self.busy = True
        self.send_btn.config(state=tk.DISABLED)
        self.accept_btn.config(text="Validating…", state=tk.DISABLED)
        self.build_btn.config(state=tk.DISABLED)
        self.status.config(text="checking design…", foreground=ui_theme.TEXT_DIM)
        candidate = dict(self.design)

        def event_sink(event: design_agent.WorkflowEvent) -> None:
            self._post(lambda e=event: self._validation_event(e))

        def worker() -> None:
            try:
                outcome = self.validator.validate(
                    candidate,
                    on_event=event_sink,
                    initial_feedback=initial_feedback,
                )
                self._post(lambda o=outcome: self._validation_done(o))
            except Exception as exc:  # noqa: BLE001
                error = f"validation worker failed: {type(exc).__name__}: {exc}"
                self._post(lambda e=error: self._validation_crashed(e))

        threading.Thread(target=worker, daemon=True).start()

    def _validation_event(self, event: design_agent.WorkflowEvent) -> None:
        if event.kind == "repair_started":
            self.status.config(text=f"phi repairing {event.attempt}/{event.max_attempts}…",
                               foreground=ui_theme.TEXT_DIM)
        elif event.kind == "check_error":
            self.card_state.config(text=event.message, foreground=ui_theme.ON_LIGHT_RED)
        elif event.kind == "validation_failed":
            count = len(event.data.get("report", {}).get("problems", [])) or 1
            self.card_state.config(
                text=(f"Found {count} blocking "
                      f"{'issue' if count == 1 else 'issues'}; sending the diagnostics "
                      "back to phi automatically…"),
                foreground=ui_theme.ON_LIGHT_AMBER,
            )
        elif event.kind == "repair_rejected":
            self.card_state.config(text=event.message, foreground=ui_theme.ON_LIGHT_AMBER)
        elif event.kind == "design_updated":
            updated = event.data.get("design")
            if isinstance(updated, dict):
                self.design = updated
                self._set_design_summary(f"Auto-repair {event.attempt}")
            self.card_state.config(text=event.message, foreground=ui_theme.TEXT_DIM)
        elif event.kind == "checking":
            self.card_state.config(text=event.message, foreground=ui_theme.TEXT_DIM)
        elif event.kind == "feedback_received":
            self.card_state.config(text=event.message, foreground=ui_theme.ON_LIGHT_AMBER)

    @staticmethod
    def _format_report(report: dict) -> str:
        if all(key in report for key in ("project", "parts", "nets", "no_connect")):
            return format_check_report(report)
        return json.dumps(report, indent=2, sort_keys=True)

    def _validation_done(self, outcome: design_agent.ValidationOutcome) -> None:
        self.validating = False
        self.busy = False
        self.send_btn.config(state=tk.NORMAL)
        self._restore_backend_status()
        self.design = dict(outcome.design)
        if outcome.last_reply:
            self.last_reply = outcome.last_reply
        self._set_design_summary("Validated design" if outcome.ok else "Design not validated")

        if outcome.ok:
            self.validated = True
            self.validated_digest = design_agent.design_digest(self.design)
            self.accept_btn.config(text="Approve design", state=tk.NORMAL)
            attempt_word = "attempt" if outcome.repairs == 1 else "attempts"
            suffix = ("No repair was needed." if outcome.repairs == 0 else
                      f"phi corrected it automatically in {outcome.repairs} {attempt_word}.")
            self.card_state.config(
                text=f"Synth validation passed. {suffix} Review the JSON, then approve.",
                foreground=ui_theme.ON_LIGHT_GREEN,
            )
            self._append("Check", "who_sys", self._format_report(dict(outcome.report)), "sys")
            return

        self.validated = False
        self.validated_digest = None
        self.approved_digest = None
        retry_state = tk.NORMAL if self.model is not None else tk.DISABLED
        retry_text = "Retry repair" if self.model is not None else "Repair unavailable"
        self.accept_btn.config(text=retry_text, state=retry_state)
        self.card_state.config(
            text=(outcome.failure or "The design did not pass automatic validation."),
            foreground=ui_theme.ON_LIGHT_RED,
        )
        self._append(
            "Check",
            "who_sys",
            self._format_report(dict(outcome.report)) +
            (f"\n\n{outcome.failure}" if outcome.failure else ""),
            "err",
        )

    def _validation_crashed(self, error: str) -> None:
        self.validating = False
        self.busy = False
        self.send_btn.config(state=tk.NORMAL)
        self._restore_backend_status()
        self.accept_btn.config(text="Retry repair", state=tk.NORMAL)
        self.card_state.config(text=error, foreground=ui_theme.ON_LIGHT_RED)
        self._append("Check", "who_sys", error, "err")

    def _accept(self) -> None:
        """Approve an already machine-validated design, or retry a stopped repair loop."""
        if self.design is None or self.busy:
            return
        digest = design_agent.design_digest(self.design)
        if not self.validated or digest != self.validated_digest:
            self._start_validation()
            return
        self.accepted = True
        self.approved_digest = digest
        self.accept_btn.config(text="Approved", state=tk.DISABLED)
        self.build_btn.config(state=tk.NORMAL)
        self.card_state.config(text="Approved — ready to build.",
                               foreground=ui_theme.ON_LIGHT_GREEN)

    # ------------------------------------------------------------------ build

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dest.get() or os.path.expanduser("~"),
                                         title="Where should the project be created?")
        if chosen:
            self.dest.delete(0, tk.END)
            self.dest.insert(0, chosen)

    def _progress_line(self, line: str) -> None:
        tag = ()
        stripped = line.strip()
        if stripped.startswith("PASS"):
            tag = ("pass",)
        elif stripped.startswith("FAIL") or stripped.startswith("error"):
            tag = ("fail",)
        self.progress.config(state=tk.NORMAL)
        self.progress.insert(tk.END, line.rstrip() + "\n", *tag)
        self.progress.config(state=tk.DISABLED)
        self.progress.see(tk.END)

    def _build(self) -> None:
        digest = design_agent.design_digest(self.design) if self.design is not None else None
        if (not self.accepted or digest != self.approved_digest or self.building
                or self.session.design_path is None):
            return
        parent = self.dest.get().strip() or DEFAULT_PROJECT_PARENT
        project = str(self.design.get("project", "design"))
        out_dir = os.path.join(os.path.expanduser(parent), project)

        try:
            root = self.session.require_root()
        except DesignError as exc:
            self._append("", "who_sys", str(exc), "err")
            return

        self.building = True
        self.build_btn.config(state=tk.DISABLED)
        self.card_state.config(text="Building… routing usually takes tens of seconds.",
                               foreground=ui_theme.TEXT_DIM)
        self.progress.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 8))
        self.progress.config(state=tk.NORMAL)
        self.progress.delete("1.0", tk.END)
        self.progress.config(state=tk.DISABLED)
        self._progress_line(f"$ phicad synth build {project} -> {out_dir}")

        def worker() -> None:
            py = design_commands.python_for_synth(root)
            cmd = [py, str(root / "phicad" / "synth" / "cli.py"), "build",
                   str(self.session.design_path), "--output", out_dir, "--force"]
            env = dict(os.environ)
            env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
            env["PYTHONUNBUFFERED"] = "1"  # stage lines must stream, not arrive in one flush
            # A build opens its own document-less api-server; it must never be pointed at
            # the session that launched this window (same contract as design_commands._run).
            env.pop("KICAD_API_SOCKET", None)
            env.pop("KICAD_API_TOKEN", None)
            lines: list[str] = []
            # This window runs synth itself rather than going through
            # DesignSession.build(), because it wants the stage lines streamed into the
            # progress pane and --json suppresses them.  That also puts it outside the
            # instrumented choke point, so it records its own -- otherwise the build a
            # user is most likely to run is the one build that never shows up.
            started = time.monotonic()

            def record(outcome: str, **extra) -> None:
                if design_commands.telemetry is None:
                    return
                design_commands.telemetry.emit(
                    "synth.run", command="build", source="new_project", streaming=True,
                    timeout_s=design_commands.BUILD_TIMEOUT_S,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    outcome=outcome, **extra)

            def stages_from(rendered: list[str]) -> dict:
                """Gate results off the streamed table.

                Without --json there is no report to read, so this parses `PASS  name`
                and `FAIL  name` out of cli.py's human rendering.  That is a display
                format, not a contract, so anything unparseable yields nothing at all
                rather than a half-count that would read as a real measurement.
                """
                seen = [m.groups() for m in
                        (re.match(r"^(PASS|FAIL)\s+(\S+)", ln) for ln in rendered) if m]
                if not seen:
                    return {}
                failed = [name for mark, name in seen if mark == "FAIL"]
                return {"stages_total": len(seen),
                        "stages_passed": len(seen) - len(failed),
                        "failed_stages": failed or None}

            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1, env=env, cwd=str(root))
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.append(line.rstrip())
                    text = line
                    self._post(lambda t=text: self._progress_line(t))
                code = proc.wait(timeout=design_commands.BUILD_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                record("timeout" if isinstance(exc, subprocess.TimeoutExpired) else "error",
                       error_type=type(exc).__name__, **stages_from(lines))
                msg = f"could not run synth: {type(exc).__name__}: {exc}"
                self._post(lambda e=msg: self._build_done(
                    False, out_dir, project, e, ""))
                return
            record("ok" if code == 0 else "failed", exit_code=code, **stages_from(lines))
            log = "\n".join(lines[-80:])
            self._post(lambda ok=code == 0, text=log: self._build_done(
                ok, out_dir, project, None, text))

        threading.Thread(target=worker, daemon=True).start()

    def _build_done(self, ok: bool, out_dir: str, project: str,
                    error: str | None, build_log: str) -> None:
        self.building = False
        self.build_btn.config(state=tk.NORMAL)
        pro = os.path.join(out_dir, f"{project}.kicad_pro")
        if error:
            self._progress_line(error)
        if ok and os.path.isfile(pro):
            self.built_pro = pro
            self.session.project_dir = Path(out_dir)
            self.build_repairs = 0        # a clean build ends this repair cycle
            self.open_btn.config(state=tk.NORMAL)
            try:
                history = project_history.prepare_generated_project(
                    out_dir, self.session.design_path)
                git_note = " " + history["git"] + "."
            except project_history.HistoryError as exc:
                git_note = f" Git history could not be initialized: {exc}"
            self.card_state.config(text="Built — every gate passed.",
                                   foreground=ui_theme.ON_LIGHT_GREEN)
            self._append("Build", "who_sys",
                         f"Project built at {out_dir}.{git_note} “Open in PhiCad” loads "
                         "it in the window you launched this from.", "sys")
        else:
            if error is not None:
                self.card_state.config(text="Build could not run; the design was not changed.",
                                       foreground=ui_theme.ON_LIGHT_RED)
                self._append("Build", "who_sys", error, "err")
                return

            if self.model is None:
                self.card_state.config(text="A build gate failed; automatic repair needs phi.",
                                       foreground=ui_theme.ON_LIGHT_RED)
                self._append(
                    "Build", "who_sys",
                    "A build gate failed. The diagnostics are already captured, but no "
                    "model adapter is available to repair the design automatically.",
                    "err",
                )
                return

            self.accepted = False
            self.approved_digest = None

            if self.build_repairs >= self.max_build_repairs:
                self.card_state.config(
                    text=(f"A build gate still fails after {self.max_build_repairs} "
                          "automatic repair rounds."),
                    foreground=ui_theme.ON_LIGHT_RED)
                self._append(
                    "Build", "who_sys",
                    f"A build gate still fails after {self.max_build_repairs} automatic "
                    "repair rounds, so PhiCad stopped retrying rather than looping. The "
                    "last gate output is above; say what to change and it will try again.",
                    "err",
                )
                return

            self.build_repairs += 1
            failed_gate = _failed_gate(build_log)
            feedback = {
                "ok": False,
                "source": "synth-build",
                "problems": [{
                    "severity": "error",
                    "where": f"build-gate.{failed_gate}" if failed_gate else "build-gate",
                    "message": (f"The validated design failed the '{failed_gate}' build gate."
                                if failed_gate else
                                "The validated design failed during synthesis."),
                    "hint": build_log or "See the build progress log.",
                }],
                "build_log": build_log,
                "build_repair_round": self.build_repairs,
                "max_build_repair_rounds": self.max_build_repairs,
            }
            self.card_state.config(
                text=("A build gate failed; sending its diagnostics to phi automatically "
                      f"({self.build_repairs}/{self.max_build_repairs})…"),
                foreground=ui_theme.ON_LIGHT_AMBER,
            )
            self._append(
                "Build", "who_sys",
                f"The '{failed_gate}' build gate failed. " if failed_gate else
                "A build gate failed. "
                + ("PhiCad captured the diagnostics and started an automatic repair "
                   f"({self.build_repairs}/{self.max_build_repairs}); you will review and "
                   "approve the revised design before it builds again."),
                "sys",
            )
            self._start_validation(initial_feedback=feedback)

    # ------------------------------------------------------------------ open in PhiCad

    def _open(self) -> None:
        if not self.built_pro:
            return
        try:
            from kipy import KiCad
            from kipy.proto.common.commands.project_commands_pb2 import (
                OpenDocument, OpenDocumentResponse)
            from kipy.proto.common.types import DocumentType

            kicad = KiCad(client_name=f"phicad-new-project-{os.getpid()}")
            kicad._client.send(
                OpenDocument(type=DocumentType.DOCTYPE_PROJECT, path=self.built_pro),
                OpenDocumentResponse)
        except Exception as exc:  # noqa: BLE001
            self._append("", "who_sys",
                         f"could not open over IPC ({type(exc).__name__}: {exc}).\n"
                         f"Open it manually: {self.built_pro}", "err")
            return
        self._append("", "who_sys", f"opened {self.built_pro} in PhiCad", "sys")

    # ------------------------------------------------------------------ event pump

    def _post(self, fn) -> None:
        self._events.put(fn)

    def _pump(self) -> None:
        try:
            while True:
                fn = self._events.get_nowait()
                try:
                    fn()
                except Exception:  # noqa: BLE001  (a worker closure must not kill the pump)
                    pass
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    NewProjectWindow().run()
