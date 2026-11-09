#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Hand control back to the PhiCad editor -- the "Manual" half of the two-mode window.

What "manual" means here, and why
---------------------------------
Agent view is this window driving its own rendered picture of the board.  Manual is the
opposite: the user works in the real PCB editor, with real tools, and this window steps
back to being a reference pane.  That split is forced by what the IPC API actually
offers, not by taste:

  * There is no viewport message in the API at all -- no zoom, pan, scroll or cursor
    command exists in `api/proto/`.  The one route to the editor's canvas is
    `RunAction`, whose own proto comment says the action names "may change as code is
    refactored" and that it is "provided for low-level prototyping purposes only"
    (api/proto/common/commands/editor_commands.proto).  Building a user-facing zoom
    control on that would break silently on an upstream refactor, so we do not.
  * Every editor-state command that does exist -- RunAction, Get/SetActiveLayer,
    Get/SetVisibleLayers, the appearance settings, HighlightNets, SyncSelection -- is
    gated by `checkForHeadless` (pcbnew/api/api_handler_board.cpp:105) and answers
    "<Command> is not available in headless mode" unless a real editor frame is
    attached.  Measured against `kicad-cli api-server`, all of them.
  * `FocusOnItem` and `RefreshEditor` are declared in the protos but have NO handler
    registered; they come back as "no handler available for request of type ...".

So there is nothing to "drive the editor with" that is both stable and testable.  What
the user actually needs from Manual mode is therefore the honest, small thing: put the
editor window in front of them.  That is a window-manager job, not an API job.

Raising the editor
------------------
KiCad raises a *plugin* it launches with System Events and the child's unix id
(common/api/api_plugin_manager.cpp:470-497).  We do the reverse, but through
`/usr/bin/open -a <bundle>` rather than System Events, because `open` needs no
Automation (TCC) permission and never shows a consent dialog on a first click.

`open -a` on a RUNNING app activates it.  On a stopped one it LAUNCHES it, which would
be a nasty surprise for someone whose window is attached to a headless api-server -- so
the process must be found first, and if none is running we refuse and say why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

# Executable names that mean "a PhiCad/KiCad GUI".  The project manager and the standalone
# editors all count: whichever one the user has in front of them is the one to raise.
_EDITOR_BINARIES = ("PhiCad", "KiCad", "kicad", "pcbnew", "eeschema")

_PS_TIMEOUT_S = 5.0
_OPEN_TIMEOUT_S = 10.0


class EditorError(RuntimeError):
    """The editor could not be reached.  The message is shown to the user verbatim."""


@dataclass
class Editor:
    """A running PhiCad GUI process and the bundle it was launched from."""

    pid: int
    exe: str                       # .../PhiCad.app/Contents/MacOS/PhiCad
    bundle: str                    # .../PhiCad.app
    source: str                    # how it was found, for the status line

    def name(self) -> str:
        return os.path.basename(self.bundle) or os.path.basename(self.exe)


def _bundle_of(exe: str) -> Optional[str]:
    """The .app a Mach-O path lives in, or None when it is not inside one."""
    marker = "/Contents/MacOS/"
    cut = exe.find(marker)
    if cut < 0:
        return None
    bundle = exe[:cut]
    return bundle if bundle.endswith(".app") else None


def _looks_like_editor(exe: str) -> bool:
    return os.path.basename(exe) in _EDITOR_BINARIES and _bundle_of(exe) is not None


def _exe_of_pid(pid: int) -> str:
    """The full executable path of a pid, or "" if it cannot be read."""
    try:
        proc = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=_PS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


def _scan_processes() -> List[Editor]:
    try:
        proc = subprocess.run(["/bin/ps", "-Ao", "pid=,comm="],
                              capture_output=True, text=True, timeout=_PS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return []

    found: List[Editor] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, exe = line.partition(" ")
        exe = exe.strip()
        if not _looks_like_editor(exe):
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        found.append(Editor(pid=pid, exe=exe, bundle=_bundle_of(exe) or "",
                            source="process list"))
    return found


def find_editor() -> Optional[Editor]:
    """The PhiCad GUI to raise, or None if no editor process is running.

    Parent process first: KiCad launches an "exec" plugin as a direct child and
    `ai_view.sh` execs python in place, so our parent IS the editor that opened us --
    which is the right window to go back to even when several PhiCads are running.
    Falls back to a scan, then to an explicit $PHICAD_APP.
    """
    parent = os.getppid()
    if parent > 1:
        exe = _exe_of_pid(parent)
        if _looks_like_editor(exe):
            return Editor(pid=parent, exe=exe, bundle=_bundle_of(exe) or "",
                          source="parent process (the editor that launched this window)")

    scanned = _scan_processes()
    if scanned:
        return scanned[0]

    override = os.environ.get("PHICAD_APP", "").strip()
    if override and os.path.isdir(override):
        # Only trusted as a last resort: an app bundle on disk is not a running editor,
        # and `open -a` would launch it.  Reported as such by raise_editor().
        return Editor(pid=0, exe=os.path.join(override, "Contents", "MacOS"),
                      bundle=override, source="$PHICAD_APP (not confirmed running)")
    return None


def raise_editor() -> str:
    """Bring the PhiCad editor window to the front.  Raises EditorError with the reason.

    Returns a sentence describing what actually happened, in the same
    say-what-really-occurred style as board_edits: no "done!" without evidence.
    """
    if sys.platform != "darwin":
        raise EditorError(
            "raising the editor window is only implemented on macOS.\n"
            "Nothing in the IPC API can focus a window, so there is no portable route; "
            "switch to PhiCad with your window manager.")

    editor = find_editor()
    if editor is None:
        raise EditorError(
            "no PhiCad editor process is running, so there is no window to raise.\n"
            "This window is most likely attached to a headless `kicad-cli api-server`. "
            "Open a board in PhiCad and relaunch this window from its toolbar button.")

    if editor.pid == 0:
        raise EditorError(
            f"$PHICAD_APP points at {editor.bundle}, but no PhiCad process is running.\n"
            "Refusing to launch it: this window would then be talking to one PhiCad while "
            "showing you another.")

    try:
        proc = subprocess.run(["/usr/bin/open", "-a", editor.bundle],
                              capture_output=True, text=True, timeout=_OPEN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EditorError(f"could not run /usr/bin/open: {exc}") from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise EditorError(
            f"open -a {editor.bundle} exited {proc.returncode}"
            + (f": {detail}" if detail else ""))

    return (f"raised {editor.name()} (pid {editor.pid}), found by {editor.source}.\n"
            "This window stays open behind it; use the toolbar button's window or "
            "Cmd-Tab to come back.")


def editor_status(kicad=None) -> str:
    """What Manual mode can and cannot do right now.  Never raises.

    `kicad` is an optional connected kipy client; when given, a GUI-only API command is
    tried so the answer distinguishes "attached to the real editor" from "attached to a
    headless api-server" instead of guessing from the socket path.
    """
    lines = [f"platform: {sys.platform}"]

    editor = find_editor()
    if editor is None:
        lines.append("editor process: none found (no PhiCad GUI is running)")
    else:
        lines.append(f"editor process: {editor.name()} pid {editor.pid} "
                     f"[{editor.source}]")
        lines.append(f"bundle: {editor.bundle}")

    socket = os.environ.get("KICAD_API_SOCKET", "") or "(default socket)"
    lines.append(f"API socket: {socket}")

    if kicad is None:
        lines.append("API frame: not probed (no connection this turn)")
        return "\n".join(lines)

    # GetActiveLayer is a stable API message that checkForHeadless gates, which makes it
    # the cheapest honest probe for "is a real editor frame on the other end".  RunAction
    # would work too but the proto declares it unstable, so it is not used anywhere here.
    try:
        board = kicad.get_board()
        layer = board.get_active_layer()
        lines.append(f"API frame: a GUI editor is attached (active layer {layer})")
    except Exception as exc:                                   # noqa: BLE001
        message = str(exc)
        if "headless" in message.lower():
            lines.append("API frame: headless api-server -- no editor window, and every "
                         "selection/layer/zoom command is rejected by design")
        elif "no handler available" in message.lower():
            # The well-known socket belongs to whichever frame started first.  When that
            # is the project manager there is no board handler at all, which reads as a
            # broken connection unless it is named.
            lines.append("API frame: connected, but this socket belongs to a frame with "
                         "no board handler (usually the project manager). Open a board "
                         "in the PCB editor and press Refresh.")
        else:
            lines.append(f"API frame: could not probe ({type(exc).__name__}: {message})")
    return "\n".join(lines)


if __name__ == "__main__":
    print(editor_status())
    try:
        print(raise_editor())
    except EditorError as error:
        print(f"raise_editor: {error}")
