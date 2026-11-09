#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Run the official KiCad Library Convention checkers as subprocesses."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, TextIO

INSTALL_COMMAND = (
    "env -u HTTPS_PROXY -u https_proxy -u ALL_PROXY -u all_proxy "
    "-u HTTP_PROXY -u http_proxy git clone "
    "https://gitlab.com/kicad/libraries/kicad-library-tools.git build/kicad-library-tools"
)
CHECKERS = {
    "symbol": "check_symbol.py",
    "footprint": "check_footprint.py",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RULE_RE = re.compile(r"\b((?:[A-Z]{1,2}\d+(?:\.\d+)+)|(?:EC\d+))\b")
HEADING_RE = re.compile(r"Checking (?:component|footprint) ['\"]([^'\"]+)['\"]")


class CheckerUnavailable(RuntimeError):
    """The external KLC checker cannot be found or started."""


def _kind_for_path(path: Path) -> str:
    if path.suffix == ".kicad_sym" and path.is_file():
        return "symbol"
    if path.suffix == ".pretty" and path.is_dir():
        return "footprint"
    raise ValueError("path must be a .kicad_sym file or .pretty directory")


def find_checker(kind: str) -> Optional[Path]:
    """Find a checker in the documented build-local clone or venv source tree."""
    name = CHECKERS[kind]
    repo = Path(__file__).resolve().parents[2]
    roots = []
    if os.environ.get("KICAD_LIBRARY_TOOLS"):
        roots.append(Path(os.environ["KICAD_LIBRARY_TOOLS"]))
    roots.extend(
        [
            repo / "build" / "kicad-library-tools",
            repo / "build" / "venv" / "src" / "kicad-library-tools",
            repo / "build" / "venv" / "src" / "kicad-library-generators",
        ]
    )
    relative_paths = (
        Path("klc-check") / name,
        Path("library_utils") / "klc-check" / name,
        Path(name),
    )
    for root in roots:
        for relative in relative_paths:
            candidate = root / relative
            if candidate.is_file():
                return candidate

    venv_script = repo / "build" / "venv" / "bin" / name
    return venv_script if venv_script.is_file() else None


def _severity(raw_line: str, line: str) -> Optional[str]:
    if re.search(r"\x1b\[(?:0;|1;)?31m", raw_line):
        return "error"
    if re.search(r"\x1b\[(?:0;)?33m", raw_line):
        return "warning"
    match = re.match(r"\s*(ERROR|WARNING)\s*[:\-]", line, re.IGNORECASE)
    return match.group(1).lower() if match else None


def parse_checker_output(
    output: str,
    tool: str,
    source: Path,
    returncode: int = 0,
    expected_checked: Optional[int] = None,
) -> dict:
    """Parse verbose checker output, including its ANSI severity colours."""
    violations = []
    current_rule = "unknown"
    current_file = str(source)
    checked = 0
    seen_headings = set()
    footprint_files = {}
    if source.is_dir():
        footprint_files = {item.stem: str(item) for item in source.glob("*.kicad_mod")}

    for raw_line in output.splitlines():
        line = ANSI_RE.sub("", raw_line).rstrip()
        heading = HEADING_RE.search(line)
        if heading:
            identity = heading.group(1)
            if identity not in seen_headings:
                seen_headings.add(identity)
                checked += 1
            if tool == CHECKERS["footprint"]:
                current_file = footprint_files.get(identity, str(source / f"{identity}.kicad_mod"))
            else:
                current_file = str(source)
            continue

        rule_match = RULE_RE.search(line)
        if "Violating" in line and rule_match:
            current_rule = rule_match.group(1)
            continue

        severity = _severity(raw_line, line)
        if severity is None:
            continue
        if rule_match:
            current_rule = rule_match.group(1)
        message = re.sub(r"^\s*(?:ERROR|WARNING)\s*[:\-]\s*", "", line, flags=re.IGNORECASE)
        if "Violating" in message and " - http" in message:
            continue
        violation = {
            "file": current_file,
            "rule": current_rule,
            "severity": severity,
            "message": message.strip(),
        }
        if violation not in violations:
            violations.append(violation)

    if checked == 0 and expected_checked is not None and returncode in (0, 2, 3):
        checked = expected_checked
    if returncode in (2, 3) and not violations:
        violations.append(
            {
                "file": str(source),
                "rule": "unknown",
                "severity": "warning" if returncode == 2 else "error",
                "message": "KLC checker reported a violation without a parseable message",
            }
        )

    return {
        "tool": tool,
        "checked": checked,
        "violations": violations,
        "ok": returncode == 0 and not violations,
    }


def run_check(
    path: Path,
    runner: Callable = subprocess.run,
    checker: Optional[Path] = None,
    finder: Callable[[str], Optional[Path]] = find_checker,
) -> dict:
    """Run the matching official checker and return a structured verdict."""
    kind = _kind_for_path(path)
    checker = checker or finder(kind)
    if checker is None:
        raise CheckerUnavailable("KLC checker is not installed")

    if kind == "symbol":
        targets = [path]
    else:
        targets = sorted(path.glob("*.kicad_mod"))
    command = [sys.executable, str(checker), "-v", *[str(target) for target in targets]]
    try:
        process = runner(command, capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError) as exc:
        raise CheckerUnavailable(str(exc)) from exc

    output = "\n".join(part for part in (process.stdout, process.stderr) if part)
    if process.returncode not in (0, 2, 3):
        detail = ANSI_RE.sub("", output).strip() or f"checker exited {process.returncode}"
        raise CheckerUnavailable(detail)
    return parse_checker_output(
        output,
        CHECKERS[kind],
        path,
        returncode=process.returncode,
        expected_checked=len(targets),
    )


def _print_human(verdict: dict, out: TextIO) -> None:
    count = len(verdict["violations"])
    print(f"{verdict['tool']}: {verdict['checked']} checked, {count} violation(s)", file=out)
    for violation in verdict["violations"]:
        print(
            f"{violation['severity'].upper()} {violation['file']} "
            f"[{violation['rule']}] {violation['message']}",
            file=out,
        )


def main(
    argv: Optional[list[str]] = None,
    *,
    runner: Callable = subprocess.run,
    finder: Callable[[str], Optional[Path]] = find_checker,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="a .kicad_sym file or .pretty directory")
    parser.add_argument("--json", action="store_true", help="print the structured verdict as JSON")
    args = parser.parse_args(argv)

    try:
        verdict = run_check(args.path, runner=runner, finder=finder)
    except ValueError as exc:
        print(f"KLC check failed: {exc}", file=err)
        return 2
    except CheckerUnavailable as exc:
        print(f"KLC checker unavailable: {exc}", file=err)
        print(INSTALL_COMMAND, file=err)
        return 2

    if args.json:
        print(json.dumps(verdict, indent=2), file=out)
    else:
        _print_human(verdict, out)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
