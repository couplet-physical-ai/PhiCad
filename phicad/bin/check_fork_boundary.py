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
"""Keep fork-only pull requests out of upstream-owned KiCad paths.

The candidate is compared with an explicit base revision.  Rename detection is disabled so
both the deleted source and added destination are inspected.  A local run also checks tracked
working-tree changes and untracked, non-ignored files; CI uses ``--committed-only`` after a clean
checkout.

A content-pinned exception list, ``phicad/fork-boundary-exceptions.json``, can admit one exact
blob at one upstream path (a fix the fork must carry before upstream does).  Pinning the blob is
what keeps such an exception one-time: the moment the file changes again -- a further edit, or
upstream's own version arriving through ``phicad sync`` -- the path is forbidden again until
the entry is deliberately revised or removed, in a reviewable ``phicad/`` diff.

Exit codes follow the other ``phicad check`` gates:

    0 = inspected and clean
    1 = inspected and found forbidden paths
    2 = could not inspect the requested repository state
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Optional

ALLOWED_PREFIXES = ("phicad/",)
ALLOWED_FILES = frozenset({
    ".github/workflows/phicad-check.yml",
    "AGENTS.md",
    "pyproject.toml",
})
EXCEPTIONS_FILE = "phicad/fork-boundary-exceptions.json"
REQUIRED_EXCEPTION_KEYS = ("path", "blob", "pr", "reason", "authorized_by")
_BLOB_ID = re.compile(r"[0-9a-f]{40}")


class BoundaryEnvironmentError(RuntimeError):
    """Git state needed to answer the boundary question was unavailable."""


class BoundaryPolicyError(RuntimeError):
    """The exception list cannot be trusted, so it must not open the gate."""


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        command = " ".join(("git", *args))
        raise BoundaryEnvironmentError(
            f"{command} failed: {detail[-1] if detail else f'exit {proc.returncode}'}"
        )
    return proc.stdout


def _object_id(repo: Path, revision: str) -> str:
    raw = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return raw.decode("ascii", errors="strict").strip()


def _paths(raw: bytes) -> set[str]:
    return {
        entry.decode("utf-8", errors="backslashreplace")
        for entry in raw.split(b"\0")
        if entry
    }


def changed_paths(repo: Path, base: str, head: str, include_worktree: bool) -> tuple[set[str], str, str]:
    """Return every path changed from *base* through the candidate state."""
    base_id = _object_id(repo, base)
    head_id = _object_id(repo, head)
    paths = _paths(_git(repo, "diff", "--name-only", "--no-renames", "-z", base_id, head_id, "--"))

    if include_worktree:
        checkout_id = _object_id(repo, "HEAD")
        if checkout_id != head_id:
            raise BoundaryEnvironmentError(
                f"candidate {head!r} is not the checked-out HEAD; use --committed-only or check out the candidate"
            )
        paths.update(_paths(_git(repo, "diff", "--name-only", "--no-renames", "-z", head_id, "--")))
        paths.update(_paths(_git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--")))

    return paths, base_id, head_id


def is_allowed(path: str) -> bool:
    """Whether *path* belongs to the fork layer or one narrow integration point."""
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        return False
    if path in ALLOWED_FILES:
        return True
    return any(path.startswith(prefix) and len(path) > len(prefix) for prefix in ALLOWED_PREFIXES)


def load_exceptions(repo: Path) -> dict:
    """Read the content-pinned exceptions; a list that cannot be trusted is a policy error.

    Every entry must name an upstream path (an exception for an already-allowed path is a
    mistake, not a no-op), a full 40-hex blob id, the pull request that carried it, the reason,
    and who authorized it -- the list is the audit record, so nothing in it may be implicit.
    """
    file = repo / EXCEPTIONS_FILE
    if not file.exists():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BoundaryPolicyError(f"{EXCEPTIONS_FILE}: {exc}")
    entries = data.get("exceptions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise BoundaryPolicyError(f"{EXCEPTIONS_FILE}: expected an object with an 'exceptions' list")

    result: dict = {}
    for index, entry in enumerate(entries):
        where = f"{EXCEPTIONS_FILE}: entry {index}"
        if not isinstance(entry, dict):
            raise BoundaryPolicyError(f"{where}: not an object")
        missing = [key for key in REQUIRED_EXCEPTION_KEYS if not entry.get(key)]
        if missing:
            raise BoundaryPolicyError(f"{where}: missing {', '.join(missing)}")
        path, blob = entry["path"], entry["blob"]
        if not isinstance(path, str) or is_allowed(path):
            raise BoundaryPolicyError(f"{where}: {path!r} is not an upstream path")
        if not (isinstance(blob, str) and _BLOB_ID.fullmatch(blob)):
            raise BoundaryPolicyError(f"{where}: blob must be a full 40-hex git object id")
        if not isinstance(entry["pr"], int):
            raise BoundaryPolicyError(f"{where}: pr must be the pull request number")
        if path in result:
            raise BoundaryPolicyError(f"{where}: {path!r} listed twice")
        result[path] = entry
    return result


def candidate_blob(repo: Path, head_id: str, path: str, include_worktree: bool) -> Optional[str]:
    """The object id of *path* in the candidate state, or None when it is deleted there."""
    if include_worktree:
        if not (repo / path).is_file():
            return None
        return _git(repo, "hash-object", "--", path).decode("ascii", errors="strict").strip()
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{head_id}:{path}"],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("ascii", errors="strict").strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="explicit base commit/ref to compare")
    parser.add_argument("--head", default="HEAD", help="candidate commit/ref (default: HEAD)")
    parser.add_argument(
        "--committed-only",
        action="store_true",
        help="ignore the checked-out working tree and untracked files (for clean CI checkouts)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(__file__).resolve().parents[2]
    try:
        paths, base_id, head_id = changed_paths(
            repo,
            args.base,
            args.head,
            include_worktree=not args.committed_only,
        )
        exceptions = load_exceptions(repo)
    except (BoundaryEnvironmentError, BoundaryPolicyError, UnicodeError) as exc:
        print(f"fork boundary: could not inspect: {exc}")
        return 2

    forbidden = []
    excepted = []
    for path in sorted(path for path in paths if not is_allowed(path)):
        entry = exceptions.get(path)
        if entry is None:
            forbidden.append(f"  {path}")
            continue
        try:
            blob = candidate_blob(repo, head_id, path, include_worktree=not args.committed_only)
        except (BoundaryEnvironmentError, UnicodeError) as exc:
            print(f"fork boundary: could not inspect: {exc}")
            return 2
        if blob == entry["blob"]:
            excepted.append(
                f"fork boundary: exception applied: {path} (blob {blob[:12]}, PR #{entry['pr']}, "
                f"authorized by {entry['authorized_by']})"
            )
        else:
            forbidden.append(
                f"  {path}  (PR #{entry['pr']}'s exception pins blob {entry['blob'][:12]}, "
                f"this content is {blob[:12] if blob else 'deleted'}: a one-time exception admits "
                f"exactly that content)"
            )

    for line in excepted:
        print(line)
    if forbidden:
        print("fork boundary: forbidden path change(s):")
        for line in forbidden:
            print(line)
        print("allowed: phicad/**, AGENTS.md, pyproject.toml, .github/workflows/phicad-check.yml")
        print("move fork-only work under phicad/; upstream synchronization is a separate workflow")
        return 1

    suffix = " plus working tree" if not args.committed_only else ""
    applied = f" ({len(excepted)} one-time exception(s) applied)" if excepted else ""
    print(
        f"fork boundary ok: {len(paths)} changed path(s), "
        f"{base_id[:12]}..{head_id[:12]}{suffix}{applied}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
