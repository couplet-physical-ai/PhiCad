#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Per-project Git history for boards created or opened through AI View.

A standalone board project gets its own repository.  A board below an existing repository
reuses that enclosing repository, while every snapshot is scoped to the board-project
subdirectory.  This avoids both silently creating nested repositories and staging unrelated
software files.  Generated sources, manual editor saves and agent edits therefore share one
audit trail, while KiCad lock/backup files and the live host-session descriptor stay local.

The module is deliberately stdlib-only.  It is copied into KiCad's user plugin directory,
where importing the PhiCad source tree is not guaranteed to work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

CONFIG_DIR = ".phicad"
CONFIG_NAME = "project.json"
BRIDGE_NAME = "host-bridge.json"
SCHEMA_VERSION = 1
PROJECT_MARKERS = (".kicad_pro", ".kicad_pcb", ".kicad_sch")
MAX_WALK_UP = 8
DEFAULT_GIT_NAME = "PhiCad Agent"
DEFAULT_GIT_EMAIL = "phicad@localhost"

# Everything here is state KiCad or PhiCad regenerates, so committing it adds noise without
# adding recoverable design history.  `.history/` matters most: KiCad's own local-history
# feature mirrors the project there and keeps its OWN git repository inside it, so leaving it
# tracked would nest a repository in ours and commit a second copy of every save.
_GITIGNORE_BLOCK = """# PhiCad runtime and KiCad transient files
.phicad/host-bridge.json
.history/
fp-info-cache
*.kicad_prl
*.lck
~*.kicad_*
*-backups/
_autosave-*
*.tmp
"""


class HistoryError(RuntimeError):
    """Git history could not be initialized or updated."""


@dataclass(frozen=True)
class HistoryStatus:
    enabled: bool
    clean: bool
    branch: str = ""
    head: str = ""
    detail: str = ""

    def display(self) -> str:
        if not self.enabled:
            return self.detail or "Git unavailable"
        state = "clean" if self.clean else "changes pending"
        revision = f" @ {self.head}" if self.head else ""
        return f"Git {self.branch or 'HEAD'}{revision} · {state}"


def _run(cwd: Path, argv: Sequence[str], timeout: float = 20.0,
         check: bool = False, env: Optional[dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, env=env if env is not None else dict(os.environ))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HistoryError(f"could not run {' '.join(argv)}: {exc}") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise HistoryError(f"{' '.join(argv)}: {detail}")
    return proc


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _has_marker(directory: Path) -> bool:
    try:
        return any(entry.is_file() and entry.suffix in PROJECT_MARKERS
                   for entry in directory.iterdir())
    except OSError:
        return False


def _path_start(path: Path | str) -> tuple[Optional[Path], str]:
    p = Path(path).expanduser()
    p = p if p.is_absolute() else (Path.cwd() / p)
    try:
        p = p.resolve()
    except OSError:
        return None, f"unresolvable path: {p}"
    if p.is_file():
        return p.parent, ""
    if p.is_dir():
        return p, ""
    if p.parent.is_dir() and p.suffix in PROJECT_MARKERS:
        return p.parent, ""
    if not p.exists():
        # A new directory is still the project named by the caller.  Probe Git from its
        # existing parent so an enclosing checkout is not missed.
        return p, ""
    return None, f"path does not exist: {p}"


def _git_repository_root(start: Path) -> Optional[Path]:
    cwd = start if start.is_dir() else start.parent
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(cwd),
                              capture_output=True, text=True, timeout=10.0,
                              env=dict(os.environ))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        return Path(proc.stdout.strip()).resolve()
    except OSError:
        return None


def repository_root(path: Path | str) -> Optional[Path]:
    """Return the nearest existing Git repository containing ``path``."""
    start, _ = _path_start(path)
    return _git_repository_root(start) if start is not None else None


def _software_repo_toplevel(start: Path) -> Optional[Path]:
    current = start
    for _ in range(MAX_WALK_UP + 4):
        if (current / ".git").exists() and (current / "phicad" / "bin" / "phicad").is_file():
            return current
        if current == current.parent:
            break
        current = current.parent
    return None


def locate_project(path: Path | str) -> tuple[Optional[Path], str]:
    """Find a safe project directory, explaining refusals to the caller.

    Existing Git always wins.  Inside an existing repository the named directory must carry
    a KiCad marker; accepting an arbitrary checkout root would make a baseline commit
    ambiguous and potentially stage unrelated software.  Outside Git, walk up to a marker so
    the standalone-project flow remains unchanged.
    """
    start, error = _path_start(path)
    if start is None:
        return None, error
    home = Path.home().resolve()
    repo = _git_repository_root(start)
    if repo is not None:
        if not _has_marker(start):
            return None, (f"{start} is inside existing repository {repo}, but it does not "
                          "identify one KiCad project; pass the board or project directory")
        candidate = start
        reason = f"using existing repository {repo} for project {candidate}"
    else:
        candidate = start
        walked = start
        for _ in range(MAX_WALK_UP):
            if _has_marker(walked):
                candidate = walked
                break
            if walked == walked.parent or walked == home:
                break
            walked = walked.parent
        reason = (f"project root ({candidate})" if _has_marker(candidate)
                  else f"no KiCad marker; using containing directory {candidate}")
    if candidate == candidate.parent:
        return None, "refusing the filesystem root"
    if candidate == home:
        return None, "refusing $HOME as a project directory"
    if (candidate / "phicad").is_dir():
        return None, f"{candidate} contains phicad/ — that is a software checkout, not a board"
    top = _software_repo_toplevel(candidate)
    if top is not None:
        return None, (f"{candidate} is inside the PhiCad source repository ({top}); "
                      "copy the project out of the source tree first")
    return candidate, reason


class ProjectHistory:
    """Initialize and snapshot one board project, local or scoped in a monorepo."""

    def __init__(self, project_dir: Path | str):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self._lock = threading.Lock()
        self._created_repository = False
        self._observed = ""
        self._stable_since = 0.0

    @property
    def metadata_dir(self) -> Path:
        return self.project_dir / CONFIG_DIR

    @property
    def config_path(self) -> Path:
        return self.metadata_dir / CONFIG_NAME

    @property
    def repository_dir(self) -> Optional[Path]:
        """The nearest existing Git toplevel, including an enclosing monorepo."""
        return repository_root(self.project_dir)

    @property
    def uses_enclosing_repository(self) -> bool:
        repo = self.repository_dir
        return bool(repo and repo != self.project_dir)

    @property
    def is_repository(self) -> bool:
        return self.repository_dir is not None

    def _guard(self) -> None:
        root, reason = locate_project(self.project_dir)
        if root is None:
            raise HistoryError(reason)

    def _write_config(self) -> None:
        if self.config_path.exists():
            return
        _atomic_json(self.config_path, {
            "schema": SCHEMA_VERSION,
            "project_root": ".",
            "version_control": {
                "provider": "git",
                "branch": "main",
                "auto_snapshot": True,
                "snapshot_after_stable_seconds": 2.0,
            },
            "agent": {
                "model": "inherit-active-phi-session",
                "transport": "host-session-or-local-phi",
            },
        })

    def _write_gitignore(self) -> None:
        path = self.project_dir / ".gitignore"
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        missing = [line for line in _GITIGNORE_BLOCK.splitlines()
                   if line and line not in previous.splitlines()]
        if not missing:
            return
        prefix = previous
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        path.write_text(prefix + _GITIGNORE_BLOCK, encoding="utf-8")

    def _init_git(self) -> None:
        # An enclosing repository is already history; never initialize a nested one.
        if self.is_repository:
            return
        self._guard()
        proc = _run(self.project_dir, ["git", "init", "-b", "main"])
        if proc.returncode != 0:
            # Git <2.28 has no `-b`.  PhiCad currently ships on newer Git, but the
            # fallback costs little and keeps imported projects portable.
            _run(self.project_dir, ["git", "init"], check=True)
            _run(self.project_dir, ["git", "checkout", "-b", "main"], check=True)
        self._created_repository = True

    def _ensure_identity(self) -> None:
        # Do not mutate an enclosing repository's local config.  `_commit_env` below supplies
        # a process-local fallback if that repository has no configured identity.
        if self._created_repository:
            name = _run(self.project_dir, ["git", "config", "--local", "user.name"])
            if name.returncode != 0 or not name.stdout.strip():
                _run(self.project_dir,
                     ["git", "config", "--local", "user.name", DEFAULT_GIT_NAME], check=True)
            email = _run(self.project_dir, ["git", "config", "--local", "user.email"])
            if email.returncode != 0 or not email.stdout.strip():
                _run(self.project_dir,
                     ["git", "config", "--local", "user.email", DEFAULT_GIT_EMAIL], check=True)

    def _commit_env(self) -> dict[str, str]:
        env = dict(os.environ)
        name = _run(self.project_dir, ["git", "config", "user.name"])
        email = _run(self.project_dir, ["git", "config", "user.email"])
        if not name.stdout.strip():
            env["GIT_AUTHOR_NAME"] = DEFAULT_GIT_NAME
            env["GIT_COMMITTER_NAME"] = DEFAULT_GIT_NAME
        if not email.stdout.strip():
            env["GIT_AUTHOR_EMAIL"] = DEFAULT_GIT_EMAIL
            env["GIT_COMMITTER_EMAIL"] = DEFAULT_GIT_EMAIL
        return env

    def import_design(self, design_path: Path | str) -> Optional[Path]:
        """Copy the design IR beside its generated project under a stable name."""
        source = Path(design_path).expanduser().resolve()
        if not source.is_file():
            return None
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            stem = str(payload.get("project") or source.stem.replace(".design", ""))
        except (OSError, ValueError, TypeError):
            stem = source.stem.replace(".design", "")
        safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in stem) or "design"
        target = self.project_dir / f"{safe}.design.json"
        if source != target:
            shutil.copy2(source, target)
        return target

    def ensure(self, design_path: Optional[Path | str] = None,
               message: str = "phicad: start project history") -> HistoryStatus:
        """Create metadata/repository and commit the current project as a baseline."""
        with self._lock:
            self._guard()
            self.project_dir.mkdir(parents=True, exist_ok=True)
            self._write_config()
            self._write_gitignore()
            if design_path is not None:
                self.import_design(design_path)
            self._init_git()
            self._ensure_identity()
            self._snapshot_unlocked(message)
            return self._status_unlocked()

    def _porcelain(self) -> str:
        if not self.is_repository:
            return ""
        proc = _run(self.project_dir,
                    ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", "."],
                    check=True)
        return proc.stdout

    def _snapshot_unlocked(self, message: str) -> bool:
        if not self.is_repository:
            raise HistoryError(f"{self.project_dir} is not a PhiCad project repository")
        if not self._porcelain().strip():
            return False
        _run(self.project_dir, ["git", "add", "-A", "--", "."], check=True)
        proc = _run(self.project_dir,
                    ["git", "commit", "--no-gpg-sign", "-m", message,
                     "--only", "--", "."], timeout=60.0, env=self._commit_env())
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "commit failed").strip()
            raise HistoryError(detail)
        self._observed = ""
        self._stable_since = 0.0
        return True

    def snapshot(self, message: str = "phicad: snapshot project changes") -> bool:
        with self._lock:
            return self._snapshot_unlocked(message)

    def autosnapshot(self, stable_seconds: float = 2.0,
                     now: Optional[float] = None) -> bool:
        """Commit once a dirty working tree has remained unchanged for a short window.

        Callers poll this from a worker thread.  A KiCad save can replace several files in
        quick succession; waiting for a stable porcelain result prevents half-save commits.
        """
        with self._lock:
            if not self.is_repository:
                return False
            stamp = self._porcelain()
            if not stamp.strip():
                self._observed = ""
                self._stable_since = 0.0
                return False
            clock = time.monotonic() if now is None else now
            if stamp != self._observed:
                self._observed = stamp
                self._stable_since = clock
                return False
            if clock - self._stable_since < stable_seconds:
                return False
            when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            return self._snapshot_unlocked(f"phicad: autosnapshot {when}")

    def _status_unlocked(self) -> HistoryStatus:
        if not self.is_repository:
            return HistoryStatus(False, True, detail="Git not initialized")
        dirty = bool(self._porcelain().strip())
        branch = _run(self.project_dir,
                      ["git", "branch", "--show-current"]).stdout.strip() or "detached"
        head_proc = _run(self.project_dir, ["git", "rev-parse", "--short", "HEAD"])
        head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
        return HistoryStatus(True, not dirty, branch=branch, head=head)

    def status(self) -> HistoryStatus:
        with self._lock:
            try:
                return self._status_unlocked()
            except HistoryError as exc:
                return HistoryStatus(False, True, detail=str(exc))


def prepare_generated_project(project_dir: Path | str, design_path: Path | str,
                              message: str = "phicad: initial generated design") -> dict:
    """Shared one-shot used by New Project, `/synth build` and the MCP tool."""
    history = ProjectHistory(project_dir)
    # Validate before copying the design so a refused source/ambiguous checkout is entirely
    # side-effect free.
    history._guard()
    imported = history.import_design(design_path)
    status = history.ensure(message=message)
    return {
        "ok": status.enabled,
        "project_dir": str(history.project_dir),
        "design": str(imported) if imported else None,
        "git": status.display(),
    }
