#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
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
"""Put `phicad/pylib` on sys.path from wherever this plugin happens to be running.

Running out of a checkout, pylib is two directories up.  Installed, it is not there
at all: `phicad ai-view-install` copies this directory to
`~/Documents/KiCad/<ver>/plugins/ai-view`, a real directory outside the repo,
because wxDir::Traverse skips symlinked directories and a symlinked plugin is never
discovered.  So `parents[2]` resolves to `~/Documents/KiCad/pylib`, which does not
exist -- and an import guarded by try/except would then quietly do nothing in the
only configuration real users run.

The installer already records the source checkout in `repo-path` for exactly this
reason (design_commands.repo_root uses it to find phicad/synth).  Same marker,
same answer.

Returns the directory it added, or None.  Never raises: a plugin that cannot find
pylib should lose telemetry, not fail to start.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent


def _candidates():
    explicit = os.environ.get("PHICAD_REPO", "").strip()
    if explicit:
        yield Path(explicit).expanduser() / "phicad" / "pylib"

    # Straight out of a checkout: phicad/plugins/ai-view -> phicad/pylib
    yield _HERE.parents[1] / "pylib"

    marker = _HERE / "repo-path"
    if marker.is_file():
        try:
            yield Path(marker.read_text(encoding="utf-8").strip()) / "phicad" / "pylib"
        except OSError:
            pass

    venv_marker = _HERE / "venv-path"
    if venv_marker.is_file():
        try:
            venv = Path(venv_marker.read_text(encoding="utf-8").strip())
        except OSError:
            return
        for parent in venv.parents:
            yield parent / "phicad" / "pylib"


def add_pylib() -> Optional[Path]:
    try:
        for candidate in _candidates():
            if (candidate / "telemetry.py").is_file():
                path = str(candidate)
                if path not in sys.path:
                    sys.path.insert(0, path)
                return candidate
    except Exception:
        pass
    return None
