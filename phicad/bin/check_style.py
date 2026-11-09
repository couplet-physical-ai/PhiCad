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
"""The mechanical style rules for the fork layer, checked without clang-format.

`phicad fmt` handles C++ through upstream's `tools/check_coding.sh`; nothing checked the
python under `phicad/`, where the house rules are a 120-column limit, no tabs for
indentation, no trailing whitespace and a newline at end of file.  Those are exactly the
rules a reviewer should never have to spend attention on, so they belong in `phicad check`
and in CI rather than in review comments.

Two deliberate scope decisions:

* only files tracked under `phicad/`, so upstream KiCad sources are never reported -- this
  fork does not reformat upstream (AGENTS.md rule 4);
* the column limit applies to CODE only.  Prose, markdown tables and JSON cannot be wrapped
  without changing what they render as, and a limit that fires 1000 times on documentation
  teaches everyone to ignore the gate.  Markdown and JSON are still checked for the rules
  that always apply: trailing whitespace and a final newline.
"""

from __future__ import annotations

import io
import subprocess
import tokenize
from pathlib import Path

MAX_COLUMNS = 120
CHECKED_SUFFIXES = {".py", ".md", ".json", ".sh", ".yml", ".yaml"}
# Where a 120-column limit is a real rule rather than a reformatting demand.
WIDTH_CHECKED_SUFFIXES = {".py", ".sh"}

# Generated or vendored files whose formatting is not ours to dictate.
SKIP_PARTS = ("__pycache__", "/vendor/", "/node_modules/")


class StyleEnvironmentError(RuntimeError):
    """The checker could not run at all — as opposed to finding a style violation.

    Kept distinct so "I could not look" never renders as "I looked and it was clean", and
    never as an unhandled traceback either.
    """


def tracked_files(repo: Path) -> list[Path]:
    # The file list comes from git on purpose: it is the only way to skip build output and
    # untracked scratch files without maintaining a second ignore list.  That makes "not a
    # git repo" a real case (an extracted tarball, a copied tree), and a CalledProcessError
    # traceback there reads like a bug in the checker rather than a missing repo.
    proc = subprocess.run(["git", "-C", str(repo), "ls-files", "phicad"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        # Quote git's diagnosis ("fatal: not a git repository ..."), not its last line: when the
        # tree sits on another mount than $HOME (Linux /tmp is usually tmpfs) git appends a
        # "Stopping at filesystem boundary" hint after the fatal line, and that hint alone
        # does not say what is wrong.
        reason = next((line for line in detail if line.startswith("fatal:")),
                      detail[-1] if detail else f"git exited {proc.returncode}")
        raise StyleEnvironmentError(
            f"cannot list tracked files under {repo}: {reason}\n"
            "       the style gate reads the file list from git, so it needs a git checkout "
            "(not an extracted archive or a copied tree).")
    out = proc.stdout
    paths = []
    for line in out.splitlines():
        if any(part in f"/{line}" for part in SKIP_PARTS):
            continue
        path = repo / line
        if path.suffix in CHECKED_SUFFIXES and path.is_file():
            paths.append(path)
    return paths


def check(path: Path, repo: Path) -> list[str]:
    rel = path.relative_to(repo)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"{rel}: cannot read: {exc}"]
    if not raw:
        return []

    problems: list[str] = []
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")

    # A missing final newline makes every later append show up as a diff on the last line.
    if not text.endswith("\n"):
        problems.append(f"{rel}: no newline at end of file")

    check_width = path.suffix in WIDTH_CHECKED_SUFFIXES
    for number, line in enumerate(lines, 1):
        if check_width and len(line) > MAX_COLUMNS:
            problems.append(f"{rel}:{number}: {len(line)} columns (limit {MAX_COLUMNS})")
        if line.rstrip() != line:
            problems.append(f"{rel}:{number}: trailing whitespace")
    if path.suffix == ".py":
        problems += _python_indent_tabs(text, rel)
    return problems


def _python_indent_tabs(text: str, rel: Path) -> list[str]:
    """Tabs used for INDENTATION, ignoring tabs inside strings.

    KiCad s-expressions are tab-indented, so a `.kicad_pcb` fixture embedded in a test as a
    string literal is full of legitimate tabs.  A naive `\\t in line` check reported 50 of
    them in synth/selftest.py -- flagging correct code, which is how a gate loses its
    credibility.  Tokenizing separates real indentation from string content.
    """
    problems: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return problems            # compileall reports syntax errors; not this gate's job
    for token in tokens:
        if token.type == tokenize.INDENT and "\t" in token.string:
            problems.append(f"{rel}:{token.start[0]}: tab indentation (use 4 spaces)")
    return problems


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    try:
        files = tracked_files(repo)
    except StyleEnvironmentError as exc:
        print(f"style: {exc}")
        return 2                   # 1 means "found violations"; 2 means "could not look"
    problems: list[str] = []
    for path in files:
        problems += check(path, repo)

    if problems:
        for problem in problems[:50]:
            print(problem)
        if len(problems) > 50:
            print(f"... and {len(problems) - 50} more")
        print(f"\n{len(problems)} style problem(s) in {len(files)} tracked files")
        return 1
    print(f"style ok: {len(files)} tracked files under phicad/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
