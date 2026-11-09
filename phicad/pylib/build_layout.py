#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
"""Where this host's build tree is and how its binaries are laid out.

The Python layer (MCP server, register/doctor, synth, support bundle, project history)
used to spell `build/mac-dev/kicad/PhiCad.app/Contents/MacOS/kicad-cli` in seven places.
On a Linux build host the tree is `build/linux-dev` and the binaries are flat
(`build/linux-dev/kicad/kicad-cli`, `build/linux-dev/pcbnew/_pcbnew.kiface`).

This module is the single Python answer, and it mirrors the shell answer in
`phicad/bin/phicad` (PLATFORM / tree_layout / built_bin):

* the platform comes from `platform.system()`, overridable with PHICAD_PLATFORM
  (tests, cross-inspection);
* the artifact layout is read from the TREE when it exists — a macOS tree copied to
  a Linux CI runner is still a bundle, a Linux tree is flat — and from the platform
  otherwise;
* PHICAD_BUILD / PHICAD_KICAD_CLI overrides are honoured by the callers, not here:
  this module only knows defaults.

No imports beyond the standard library, so anything under phicad/ can use it.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Union

PathLike = Union[str, "os.PathLike[str]"]

#: Sub-path of the macOS app bundle's executables inside a build tree.
BUNDLE_MACOS = ("kicad", "PhiCad.app", "Contents", "MacOS")


def platform_name() -> str:
    """'mac', 'linux' or 'other' — PHICAD_PLATFORM wins over the running system."""
    override = os.environ.get("PHICAD_PLATFORM", "").strip().lower()
    if override in ("mac", "darwin"):
        return "mac"
    if override == "linux":
        return "linux"
    system = platform.system()
    if system == "Darwin":
        return "mac"
    if system == "Linux":
        return "linux"
    return "other"


def default_build_dir(repo: PathLike, flavor: str = "dev") -> Path:
    """`<repo>/build/<platform>-<flavor>` — what `phicad configure <flavor>` creates here."""
    return Path(repo) / "build" / "{}-{}".format(platform_name(), flavor)


def layout_of(build: PathLike) -> str:
    """'bundle' or 'flat'. The tree decides when it exists; the platform decides otherwise."""
    build = Path(build)
    if platform_name() == "mac" or build.joinpath(*BUNDLE_MACOS[:2]).is_dir():
        return "bundle"
    return "flat"


def kicad_cli_in(build: PathLike) -> Path:
    """The `kicad-cli` a build tree holds (whether or not it has been built yet)."""
    build = Path(build)
    if layout_of(build) == "bundle":
        return build.joinpath(*BUNDLE_MACOS, "kicad-cli")
    return build / "kicad" / "kicad-cli"


def gui_in(build: PathLike) -> Path:
    """The GUI executable: `PhiCad` inside the bundle, upstream's `kicad` name when flat."""
    build = Path(build)
    if layout_of(build) == "bundle":
        return build.joinpath(*BUNDLE_MACOS, "PhiCad")
    return build / "kicad" / "kicad"


def kicad_cli_candidates(build: PathLike) -> tuple:
    """Every path a kicad-cli could occupy in a tree, most specific first.

    For code that probes rather than predicts (project history, support bundles): the
    fork's bundle, an upstream-named bundle, then the flat Linux path.
    """
    build = Path(build)
    return (
        build.joinpath(*BUNDLE_MACOS, "kicad-cli"),
        build.joinpath("kicad", "KiCad.app", "Contents", "MacOS", "kicad-cli"),
        build / "kicad" / "kicad-cli",
    )
