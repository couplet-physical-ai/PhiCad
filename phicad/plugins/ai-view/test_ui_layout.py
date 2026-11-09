#!/usr/bin/env python3
#
# Copyright (C) 2026 Exowatt, Inc. / PhiCad contributors
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
"""Layout tests for the AI View chrome: does the text fit the space it is given?

Screenshots prove a layout was right once, on one window size. These assert the property
that mattered: **nothing in the mode bar is silently clipped**.

The bug this pins: the mode-bar status line was one f-string in a `ttk.Label` with
`anchor="w"` and no wrapping, so when the bar ran out of room Tk cut the end off. On a
1512 px window -- the widest common MacBook -- it read "Git waitin", clipped mid-word, and
an ordinary state (board name + model + running cost) wanted 592 px in a 416 px slot. A
truncated status line is worse than a short one, because the reader cannot tell a cut-off
word from a real value.

Tk is absent on CI runners, so these self-skip there rather than failing.

Run: PYTHONPATH=. python3 test_ui_layout.py
"""
from __future__ import annotations

import subprocess
import sys
import types
import unittest

try:
    import tkinter as tk
    from tkinter import ttk
    from tkinter import font as tkfont
    _TK_IMPORT_ERROR = ""
except Exception as exc:                       # noqa: BLE001 - any Tk failure means skip
    _TK_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if _TK_IMPORT_ERROR:
    raise unittest.SkipTest(f"Tk unavailable ({_TK_IMPORT_ERROR})")

import ai_view_gui


def _tk_available() -> tuple[bool, str]:
    """Probe Tk in a child because macOS can abort instead of raising when GUI bootstrap is absent."""
    code = "import tkinter as tk; root = tk.Tk(); root.destroy()"
    try:
        probe = subprocess.run(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    if probe.returncode != 0:
        return False, f"probe exited {probe.returncode}"
    return True, ""


_HAVE_TK, _WHY_NOT = _tk_available()


class _StatusLine:
    """The real renderer, bound to a real label, without building the whole app.

    Instantiating AiViewApp would need a board, a backend and a config home; the fitting
    logic needs only the label and the segment list, so the test exercises exactly that.
    """

    _STATUS_SEP = ai_view_gui.AiViewApp._STATUS_SEP
    _render_status_compact = ai_view_gui.AiViewApp._render_status_compact

    def __init__(self, label: "ttk.Label", segments: list[str]) -> None:
        self.status_compact = label
        self._status_segments = segments


@unittest.skipUnless(_HAVE_TK, f"Tk unavailable ({_WHY_NOT})")
class ModeBarStatusFitTest(unittest.TestCase):
    # Least to most important, as _update_status builds it.
    SEGMENTS = ["Git waiting", "phi ready", "IPC client error", "(no board open)"]

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.label = ttk.Label(self.root, text="", font=(family, 11), anchor="w")
        self.font = tkfont.Font(font=self.label.cget("font"))
        self.addCleanup(self.root.destroy)

    def render(self, width: int, segments: list[str] | None = None) -> str:
        _StatusLine(self.label, list(self.SEGMENTS if segments is None else segments)) \
            ._render_status_compact(width)
        return self.label.cget("text")

    def test_it_fits_the_width_it_is_given(self) -> None:
        """The measured property, at the width the real mode bar hands the label."""
        for width in (416, 380, 300, 240, 200):
            text = self.render(width)
            self.assertLessEqual(
                self.font.measure(text), width,
                f"status line overflows {width}px: {text!r}")

    def test_the_most_important_facts_survive_a_narrow_window(self) -> None:
        """Board and IPC state are what the window is about; they go last."""
        self.assertIn("(no board open)", self.render(300))
        self.assertIn("IPC client error", self.render(300))

    def test_dropping_a_segment_is_disclosed(self) -> None:
        """An ellipsis, so a shortened line cannot be read as the whole truth."""
        full = self.render(1000)
        self.assertNotIn("…", full, "nothing was dropped, so nothing should be marked")
        self.assertIn("Git waiting", full)

        narrowed = self.render(416)
        self.assertIn("…", narrowed)
        self.assertNotIn("Git waiting", narrowed)

    def test_no_segment_is_cut_mid_word(self) -> None:
        """The original bug: 'Git waiting' rendered as 'Git waitin'."""
        for width in (416, 380, 300, 240, 200, 160):
            text = self.render(width).removesuffix(" …")
            for shown in text.split(self._sep()):
                self.assertIn(shown.strip(), self.SEGMENTS,
                              f"{shown.strip()!r} is not a whole segment (width {width})")

    def _sep(self) -> str:
        return ai_view_gui.AiViewApp._STATUS_SEP

    def test_the_costly_state_fits_too(self) -> None:
        """A model name and a running cost is ordinary, and used to need 592px of 416."""
        segments = ["Git 3 commits", "phi ready · gateway/strong · $0.0412",
                    "IPC connected", "sensor-node"]
        text = self.render(416, segments)
        self.assertLessEqual(self.font.measure(text), 416)
        self.assertIn("sensor-node", text)

    def test_an_unmapped_label_shows_everything(self) -> None:
        """Before the first <Configure>, winfo_width() is 1: do not drop everything."""
        text = self.render(1)
        self.assertIn("Git waiting", text)
        self.assertNotIn("…", text)

    def test_one_segment_is_never_dropped_to_nothing(self) -> None:
        """Absurdly narrow: keep the single most important fact, do not blank the line."""
        text = self.render(10)
        self.assertTrue(text.strip(), "status line went empty")
        self.assertIn("(no board open)", text)


class RefreshNoteDeduplicationTest(unittest.TestCase):
    """The label beside Refresh must not repeat what the status line already says.

    The bar read "... IPC client error ... [client error] Refresh" -- one fact, twice, in
    two phrasings, which reads like two separate problems. The rule: the status line owns
    connection state, the label owns the outcome of the last refresh attempt.
    """

    @staticmethod
    def shown(note: str, ipc_state: str) -> str:
        """The rule as _finish_refresh applies it."""
        return "" if note == ipc_state else note

    def test_a_note_identical_to_the_ipc_state_is_suppressed(self) -> None:
        for state in ("client error", "not connected", "connect failed"):
            self.assertEqual(self.shown(state, state), "",
                             f"{state!r} is already in the status line")

    def test_the_timing_note_survives(self) -> None:
        """'ok - read in 0.42s' is information the status line does not carry."""
        self.assertEqual(self.shown("ok - read in 0.42s", "connected"),
                         "ok - read in 0.42s")

    def test_no_board_open_survives_when_the_socket_is_fine(self) -> None:
        """Connected but nothing open is a different fact from the connection state."""
        self.assertEqual(self.shown("no board open", "connected"), "no board open")


class NoBoardMessageTest(unittest.TestCase):
    """Why there is nothing to draw must match the reason, not always blame the editor.

    board_paths() returns None for four different reasons and the render panes could not
    tell them apart, so every empty view said "Open a board in PhiCad, then press Refresh".
    Observed failing: PhiCad WAS running with a board open, and the real blocker was
    `No module named 'kipy'` -- the Context tab diagnosed it correctly while the PCB tab
    sent the reader to the editor.
    """

    class _Ctx:
        def __init__(self, board: bool, name: str = "", host: str = "") -> None:
            self._board, self.board_name, self.host = board, name, host
            self.project_path = ""

        def has_board(self) -> bool:
            return self._board

    def message(self, ctx: object, ipc_state: str) -> str:
        app = types.SimpleNamespace(ctx=ctx, ipc_state=ipc_state)
        return ai_view_gui.AiViewApp.no_board_message(app)

    def test_a_dead_client_is_not_reported_as_a_missing_board(self) -> None:
        text = self.message(None, "client error")
        self.assertIn("connection problem", text)
        self.assertNotIn("Open a board", text)

    def test_it_points_at_the_tab_that_has_the_diagnosis(self) -> None:
        self.assertIn("Context tab", self.message(None, "client error"))

    def test_still_connecting_is_not_an_error(self) -> None:
        text = self.message(None, "connecting...")
        self.assertIn("Not connected", text)
        self.assertNotIn("connection problem", text)

    def test_connected_with_nothing_open_does_say_open_a_board(self) -> None:
        text = self.message(self._Ctx(False), "connected")
        self.assertIn("no board is open", text.lower())
        self.assertIn("Open a board", text)

    def test_the_schematic_host_is_told_the_pcb_is_missing(self) -> None:
        text = self.message(self._Ctx(False, host="schematic"), "connected")
        self.assertIn("no saved", text)
        self.assertIn("PCB", text)

    def test_an_unsaved_board_says_so_by_name(self) -> None:
        text = self.message(self._Ctx(True, name="untitled.kicad_pcb"), "connected")
        self.assertIn("untitled.kicad_pcb", text)
        self.assertIn("never been saved", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
