#!/usr/bin/env python3
"""Validate and launch a staged PhiCad macOS release bundle.

This is the cheap, deterministic half of ``phicad install``.  The wrapper owns
building and staging; this file checks the installed shape and launches the
installed CLI without any build-tree environment overrides.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence


class SmokeError(RuntimeError):
    """The staged bundle is incomplete or cannot launch independently."""


REQUIRED_FILES = (
    "Contents/Info.plist",
    "Contents/PlugIns/_pcbnew.kiface",
    "Contents/PlugIns/_eeschema.kiface",
    "Contents/SharedSupport/plugins/ai-view/plugin.json",
)
REQUIRED_DIRECTORIES = (
    "Contents/Frameworks",
    "Contents/SharedSupport",
)
REQUIRED_EXECUTABLES = (
    "Contents/MacOS/PhiCad",
    "Contents/MacOS/kicad-cli",
)
BUILD_TREE_ENV = (
    "DYLD_FRAMEWORK_PATH",
    "DYLD_LIBRARY_PATH",
    "KICAD_BUILD_PATHS",
    "KICAD_CONFIG_HOME",
    "KICAD_RUN_FROM_BUILD_DIR",
    "KICAD_STOCK_DATA_HOME",
    "PHICAD_BUILD",
    "PHICAD_KICAD_CLI",
)

Runner = Callable[..., subprocess.CompletedProcess]


def _run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(args, **kwargs)  # type: ignore[arg-type]


def _clean_launch_env(source: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if source is None else source)
    for name in BUILD_TREE_ENV:
        env.pop(name, None)
    return env


def _validate_shape(app: Path) -> None:
    problems: List[str] = []
    if not app.is_dir():
        raise SmokeError(f"installed app is missing: {app}")

    for relative in REQUIRED_FILES:
        path = app / relative
        if not path.is_file():
            problems.append(f"missing file: {relative}")
    for relative in REQUIRED_DIRECTORIES:
        path = app / relative
        if not path.is_dir() or path.is_symlink():
            problems.append(f"missing installed directory (or still a build-tree symlink): {relative}")
    for relative in REQUIRED_EXECUTABLES:
        path = app / relative
        if not path.is_file() or not os.access(path, os.X_OK):
            problems.append(f"missing executable: {relative}")

    plist_path = app / "Contents/Info.plist"
    if plist_path.is_file():
        try:
            with plist_path.open("rb") as stream:
                executable = plistlib.load(stream).get("CFBundleExecutable")
        except (OSError, plistlib.InvalidFileException) as exc:
            problems.append(f"invalid Contents/Info.plist: {exc}")
        else:
            if executable != "PhiCad":
                problems.append(f"CFBundleExecutable is {executable!r}, expected 'PhiCad'")

    if problems:
        raise SmokeError("installed bundle is incomplete:\n  " + "\n  ".join(problems))


def smoke_bundle(
    app: Path,
    *,
    runner: Runner = _run,
    platform: Optional[str] = None,
    launch_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Validate ``app``, verify its signature on macOS, and run its CLI."""
    app = app.resolve()
    _validate_shape(app)
    active_platform = sys.platform if platform is None else platform

    if active_platform == "darwin":
        signed = runner(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if signed.returncode:
            detail = (signed.stderr or signed.stdout or "codesign returned no diagnostic").strip()
            raise SmokeError(f"installed bundle signature is invalid: {detail}")

    cli = app / "Contents/MacOS/kicad-cli"
    launched = runner(
        [str(cli), "--version"],
        cwd=str(app.parent),
        env=_clean_launch_env(launch_env),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if launched.returncode:
        detail = (launched.stderr or launched.stdout or "kicad-cli returned no diagnostic").strip()
        raise SmokeError(f"installed kicad-cli --version failed ({launched.returncode}): {detail}")

    version = launched.stdout.strip().splitlines()
    if not version:
        raise SmokeError("installed kicad-cli --version returned no version")

    return {
        "app": str(app),
        "version": version[0],
        "signature": "verified" if active_platform == "darwin" else "not checked (non-macOS test host)",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="installed PhiCad.app to validate")
    args = parser.parse_args(argv)

    try:
        result = smoke_bundle(args.app)
    except SmokeError as exc:
        print(f"release smoke failed: {exc}", file=sys.stderr)
        return 1

    print("release smoke passed")
    print(f"  app       : {result['app']}")
    print(f"  kicad-cli : {result['version']}")
    print(f"  signature : {result['signature']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
