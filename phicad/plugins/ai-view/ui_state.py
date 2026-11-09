#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Remember how the AI View window was left: Agent or Manual, chat open, which tab.

Why a file of our own rather than KiCad's settings
--------------------------------------------------
The obvious home is the IPC call `GetPluginSettingsPath`, but that needs a live
connection, and the window has to lay itself out *before* the first IPC round trip --
and must still lay itself out correctly when there is no connection at all.  So the
path is computed client-side from the same environment KiCad itself reads.

Where it lands (`state_path()` prints it, and so does `/view state`):

    $PHICAD_AI_STATE                     an explicit file; also the test hook
    $KICAD_CONFIG_HOME/phicad/ai-view.json
    macOS   ~/Library/Preferences/kicad/phicad/ai-view.json
    Windows %APPDATA%\\kicad\\phicad\\ai-view.json
    else    ${XDG_CONFIG_HOME:-~/.config}/kicad/phicad/ai-view.json

Deliberately NOT under the `10.99/` version directory KiCad uses for its own settings:
"I like the agent view maximised" is a preference about a person, not about a file
format, and it should survive a version bump.

Nothing here may raise.  A read-only home directory must cost the user a preference,
never the window -- so every failure is captured in `last_error` for the UI to report
honestly instead of being thrown.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass

MODE_AGENT = "agent"
MODE_MANUAL = "manual"
MODES = (MODE_AGENT, MODE_MANUAL)

_FILE = "ai-view.json"


def _config_root() -> str:
    """KiCad's per-user configuration directory, by the same rules KiCad uses.

    Mirrors PATHS::CalculateUserSettingsPath (common/paths.cpp) minus the version
    directory, so a session started with KICAD_CONFIG_HOME set (QA, a sandboxed test)
    gets its own state rather than scribbling on the real one.
    """
    explicit = os.environ.get("KICAD_CONFIG_HOME", "").strip()
    if explicit:
        return explicit
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Preferences/kicad")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "kicad")
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.expanduser("~/.config")
    return os.path.join(xdg, "kicad")


def state_path() -> str:
    """The file the window's layout preference is read from and written to."""
    override = os.environ.get("PHICAD_AI_STATE", "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(_config_root(), "phicad", _FILE)


@dataclass
class UiState:
    """What survives a relaunch.  Everything else is derived on startup."""

    mode: str = MODE_MANUAL
    chat_open: bool = True
    tab: str = ""                 # tab label, lower case; "" = whatever opens first
    last_error: str = ""          # why the last load/save failed, for the UI to say

    def normalised(self) -> "UiState":
        if self.mode not in MODES:
            self.mode = MODE_MANUAL
        self.chat_open = bool(self.chat_open)
        self.tab = str(self.tab or "").strip().lower()
        return self


def load() -> UiState:
    """Read the saved layout.  A missing or corrupt file is a fresh default, not an error.

    A corrupt file IS recorded in `last_error` though: silently starting over is how a
    preference file that can never be written appears to work forever.
    """
    path = state_path()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return UiState()
    except (OSError, ValueError) as exc:
        return UiState(last_error=f"could not read {path}: {exc}")

    if not isinstance(raw, dict):
        return UiState(last_error=f"{path} does not contain a JSON object")

    state = UiState(
        mode=str(raw.get("mode", MODE_MANUAL)),
        chat_open=bool(raw.get("chat_open", True)),
        tab=str(raw.get("tab", "")),
    )
    return state.normalised()


def save(state: UiState) -> str:
    """Write the layout back.  Returns "" on success, or the failure text to show."""
    path = state_path()
    payload = asdict(state.normalised())
    payload.pop("last_error", None)          # a failure is not itself a preference
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)                # atomic: a killed window never leaves a stub
    except OSError as exc:
        return f"could not save the window layout to {path}: {exc}"
    return ""


if __name__ == "__main__":
    print(f"state file: {state_path()}")
    print(load())
