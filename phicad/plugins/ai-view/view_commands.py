#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Chat commands that drive the AI View window itself -- "control that plane".

Why these exist, and what they can honestly reach
-------------------------------------------------
The obvious reading of "let the assistant move the view" is that chat should scroll and
zoom the PCB editor's canvas.  It cannot: the IPC API has no viewport message of any
kind, and the one generic escape hatch (`RunAction`) is declared unstable in its own
proto.  See editor_control.py for the full evidence.

But the AI View window draws its OWN picture of the board -- the PCB / Schematic / 3D
tabs, rendered by kicad-cli, with their own layer sets, sides and zoom.  That plane is
entirely ours, needs no editor, works against a headless api-server, and is exactly what
Agent view puts on screen.  So that is the plane chat controls.

The contract is board_edits.py's, unchanged
-------------------------------------------
    the MODEL recommends a command  ->  the USER types it  ->  we run it  ->  we report
    what the window ACTUALLY shows afterwards

A slash command is intercepted in the GUI and never reaches the model, so the model
cannot move the view on its own even if it tried.  This module only PARSES; every verb is
executed by AiViewApp, which reads the resulting state back out of the widgets rather
than assuming the change took -- a `/view tab 3d` whose render then fails must say the
render failed, not "showing 3D".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

_PREFIXES = ("/view", "/mode", "/chat", "/editor")

# tab alias -> the ImageTab key ("" = the text Context tab, which is not an ImageTab)
TABS: Dict[str, str] = {
    "context": "", "ctx": "", "text": "", "digest": "",
    "pcb": "pcb", "board": "pcb", "2d": "pcb", "layout": "pcb",
    "sch": "sch", "schematic": "sch", "schem": "sch",
    "3d": "3d", "three": "3d", "3-d": "3d",
}

# PCB layer-set alias -> the label in ai_view_gui.VIEW_SPECS["pcb"]
LAYER_SETS: Dict[str, str] = {
    "front": "Front", "f": "Front", "top": "Front",
    "back": "Back", "b": "Back", "bottom": "Back",
    "both": "Both", "all": "Both",
}

# 3D side alias -> the label in ai_view_gui.VIEW_SPECS["3d"]
SIDES: Dict[str, str] = {
    "top": "Top", "up": "Top",
    "bottom": "Bottom", "down": "Bottom", "under": "Bottom",
    "front": "Front", "side": "Front",
    "left": "Left",
}

MODES = ("agent", "manual", "toggle")
CHAT_STATES = ("open", "close", "toggle")
EDITOR_VERBS = ("raise", "status")

# The command table is the single source of truth: /help renders it, the entry box's
# completion popup filters it, and HELP below is generated from it.  Three copies of the
# same list is how a command ends up documented in one place and missing from another.
#   (name, argument spelling, one line the user can act on)
COMMANDS = (
    ("/view", "", "report what is on screen right now"),
    ("/view", "pcb | sch | 3d | context", "show that tab"),
    ("/view layers", "front | back | both", "which copper set the PCB tab plots"),
    ("/view side", "top | bottom | front | left", "which angle the 3D tab renders"),
    ("/view zoom", "in | out | fit | <factor>", "zoom the visible tab, e.g. 1.5"),
    ("/view fit", "", "fit the visible tab to the pane"),
    ("/view render", "", "re-render the visible tab from the board file"),
    ("/mode", "agent | manual", "agent = the view fills the window, chat floats on it"),
    ("/chat", "open | close", "show or hide the floating chat panel (agent mode)"),
    ("/editor raise", "", "bring the PhiCad editor window to the front"),
    ("/editor status", "", "what this window can and cannot reach right now"),
)


def _help() -> str:
    body = "\n".join(f"  {name} {args}".ljust(38) + summary
                     for name, args, summary in COMMANDS)
    return (
        "View commands (these move THIS window's rendered view; they never touch the "
        "board):\n\n" + body + "\n\n"
        "The assistant can recommend any of these but cannot run them: a slash command "
        "is intercepted before the model ever sees it. You type it, we run it, we tell "
        "you what the window actually ended up showing.")


HELP = _help()


class ViewError(RuntimeError):
    """The command was rejected.  The message carries the usage line."""


@dataclass
class ViewCommand:
    """One parsed command: a verb the GUI dispatches on, plus its already-validated arg."""

    verb: str                 # tab|layers|side|zoom|render|state|help|mode|chat|editor
    arg: str = ""             # the canonical label, e.g. "pcb", "Front", "agent"
    factor: float = 0.0       # /view zoom <factor> only


def is_command(line: str) -> bool:
    return line.strip().split(" ", 1)[0].lower() in _PREFIXES


def _one_of(word: str, table, what: str, usage: str) -> str:
    key = word.strip().lower()
    if isinstance(table, dict):
        if key in table:
            return table[key]
        known = ", ".join(sorted(set(table)))
    else:
        if key in table:
            return key
        known = ", ".join(table)
    raise ViewError(f"{what} {word!r} is not one of: {known}\nusage: {usage}")


def parse(line: str) -> ViewCommand:
    """Parse one /view, /mode, /chat or /editor command.  Raises ViewError with usage."""
    parts = line.strip().split()
    if not parts:
        raise ViewError("empty command")
    head, args = parts[0].lower(), parts[1:]
    first = args[0].lower() if args else ""

    if head == "/mode":
        if not first:
            raise ViewError("usage: /mode agent | manual | toggle")
        return ViewCommand("mode", _one_of(first, MODES, "mode",
                                           "/mode agent | manual | toggle"))

    if head == "/chat":
        if not first:
            raise ViewError("usage: /chat open | close | toggle")
        return ViewCommand("chat", _one_of(first, CHAT_STATES, "chat state",
                                           "/chat open | close | toggle"))

    if head == "/editor":
        if not first:
            return ViewCommand("editor", "status")
        return ViewCommand("editor", _one_of(first, EDITOR_VERBS, "editor verb",
                                             "/editor raise | status"))

    # ---------------------------------------------------------------- /view
    if not first or first == "state":
        return ViewCommand("state")
    if first in ("help", "?"):
        return ViewCommand("help")
    if first == "render":
        return ViewCommand("render")
    if first == "fit":
        return ViewCommand("zoom", "fit")

    if first == "tab":
        if len(args) != 2:
            raise ViewError("usage: /view tab context|pcb|sch|3d")
        return ViewCommand("tab", _one_of(args[1], TABS, "tab",
                                          "/view tab context|pcb|sch|3d"))

    if first == "layers":
        if len(args) != 2:
            raise ViewError("usage: /view layers front|back|both")
        return ViewCommand("layers", _one_of(args[1], LAYER_SETS, "layer set",
                                             "/view layers front|back|both"))

    if first == "side":
        if len(args) != 2:
            raise ViewError("usage: /view side top|bottom|front|left")
        return ViewCommand("side", _one_of(args[1], SIDES, "side",
                                           "/view side top|bottom|front|left"))

    if first == "zoom":
        if len(args) != 2:
            raise ViewError("usage: /view zoom in|out|fit|<factor>")
        word = args[1].lower()
        if word in ("in", "out", "fit"):
            return ViewCommand("zoom", word)
        try:
            factor = float(word)
        except ValueError:
            raise ViewError("usage: /view zoom in|out|fit|<factor>   "
                            f"(got {args[1]!r})") from None
        # A zoom of 0 or a negative one is not a smaller picture, it is a crash in the
        # resize; and beyond 20x a 1600 px plot is 32000 px of resampling per redraw.
        if not 0.05 <= factor <= 20.0:
            raise ViewError(f"zoom factor {factor:g} is outside 0.05 .. 20")
        return ViewCommand("zoom", "factor", factor)

    # Bare tab names are the common case ("/view 3d"), so accept them last -- after the
    # keywords, so a future keyword can never be shadowed by a tab alias.
    if first in TABS:
        if len(args) != 1:
            raise ViewError(f"usage: /view {first}   (takes no arguments)")
        return ViewCommand("tab", TABS[first])

    raise ViewError(f"unknown /view command {first!r}.\n\n{HELP}")


def describe_zoom(zoom: float, fit_zoom: float) -> Tuple[str, str]:
    """(absolute, relative-to-fit) descriptions of a tab's zoom, for the report line."""
    if fit_zoom <= 0:
        return (f"{zoom:.2f}x", "unknown")
    return (f"{zoom:.2f}x", f"{zoom / fit_zoom:.2f}x fit")
