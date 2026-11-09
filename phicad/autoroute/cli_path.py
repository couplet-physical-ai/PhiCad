#!/usr/bin/env python3
"""Resolve the fork's kicad-cli, the same way for every tool in this package.

There were three copies of this logic and they had already drifted.  freeroute.py
honoured $KICAD_CLI and walked up to the repo's build tree; cleanup.py and
stitch.py only looked at PATH, so the documented invocation

    python3 phicad/autoroute/cleanup.py board.kicad_pcb

died with "kicad-cli not on PATH" even with KICAD_CLI exported -- which is exactly
what `phicad autoroute` exports for them.  On a machine where stock KiCad has been
removed (the normal state for this fork) there is no kicad-cli on PATH at all, so
that was not an edge case, it was the default experience.

Order matters, and PATH is deliberately LAST:

  1. $KICAD_CLI, if it points at something that exists.  An explicit request wins.
  2. the repo's own build tree, found by walking up from this file.  This is the
     binary a checkout is meant to use.
  3. PATH, as a courtesy for an installed build.

PATH goes last because a *stock* kicad-cli found there is worse than no binary at
all: this fork writes version 20260831 files, and a stock 10.0.x cli will either
refuse them or quietly read them as something else.  Silently preferring it would
turn a clear "not found" into a confusing wrong answer.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: Build-tree layouts to try, relative to a repo root.
_BUILD_DIRS = ("mac-dev", "mac-release", "mac-debug", "linux-dev", "build")
_APP_NAMES = ("PhiCad", "KiCad")


def _from_env() -> str | None:
    # Both spellings: KICAD_CLI is the qa convention, PHICAD_KICAD_CLI is what
    # the MCP server and synth read — honoring only one made the same shell
    # environment work for `synth build` and fail for autoroute.
    for name in ("KICAD_CLI", "PHICAD_KICAD_CLI"):
        env = os.environ.get(name)
        if env and Path(env).exists():
            return env
    return None


def _from_build_tree(start: Path) -> str | None:
    for parent in start.resolve().parents:
        for build in _BUILD_DIRS:
            # macOS keeps the binary inside the .app bundle; Linux/Windows do not.
            for app in _APP_NAMES:
                cand = (parent / "build" / build / "kicad" / f"{app}.app"
                        / "Contents" / "MacOS" / "kicad-cli")
                if cand.exists():
                    return str(cand)
            for name in ("kicad-cli", "kicad-cli.exe"):
                cand = parent / "build" / build / "kicad" / name
                if cand.exists():
                    return str(cand)
    return None


def _from_path() -> str | None:
    for name in ("kicad-cli", "kicad-cli.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def find_cli(explicit: str | None = None) -> str:
    """Return a usable kicad-cli path, or exit with advice that names every option.

    `explicit` is a --cli argument and outranks everything, but it is checked for
    existence so a typo fails here rather than as a confusing exec error later.
    """
    if explicit:
        if not Path(explicit).exists():
            raise SystemExit("--cli %s does not exist" % explicit)
        return explicit

    for source in (_from_env, lambda: _from_build_tree(Path(__file__))):
        found = source()
        if found:
            return found

    found = _from_path()
    if found:
        return found

    raise SystemExit(
        "kicad-cli not found.  Any one of these fixes it:\n"
        "  * pass --cli <path>\n"
        "  * export KICAD_CLI=<path to the fork's kicad-cli>\n"
        "  * build the fork (phicad build kicad-cli), so the build tree is found\n"
        "  * put an installed kicad-cli on PATH\n"
        "Note a STOCK kicad-cli cannot read this fork's boards, so PATH is tried last."
    )


if __name__ == "__main__":  # a tiny probe, so the resolver is testable by hand
    print(find_cli(sys.argv[1] if len(sys.argv) > 1 else None))
