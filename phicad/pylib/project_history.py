#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Per-project Git history and an append-only journal for KiCad projects PhiCad touches.

A board project uses the nearest safe Git repository.  A standalone project gets a
repository at its own root; a project below an existing repository stays in that enclosing
repository and all snapshots are scoped to the project directory.  This avoids both silently
creating nested repositories and staging unrelated software files.  Generated sources, manual
editor saves and agent edits therefore share one audit trail, while KiCad lock/backup files and
the live host-session descriptor stay local.

THREE CALLERS, ONE FILE
    * `phicad/mcp/tools_git.py` imports it (by file path) for the project_* MCP tools and
      for the coordinator's `record_and_snapshot()` hook after every mutating tool.
    * `phicad/bin/phicad` shells to it: `python3 -m project_history <cmd> --json`, or as a
      plain script.  Both entry points work (see `main()`).
    * The ai-view plugin imports it as a bare module (it is copied into KiCad's user plugin
      directory), which is why this file is stdlib-only and 3.9-safe: the interpreter that
      loads a KiCad plugin is not ours to choose, and the automation venv is not on its path.

WHAT IS SAFE AND WHAT IS REFUSED
    `locate_project()` identifies the directory the caller actually named and
    `repository_root()` identifies the nearest existing Git repository.  An existing
    repository is always reused; `git init` is only allowed when no Git ancestor exists.
    A path inside an existing repository must contain a KiCad project marker itself, so a
    request for an enclosing checkout with no unambiguous project is refused rather than
    staging the checkout by accident.  Anything in the PhiCad source checkout is also
    refused: a directory containing `phicad/`, or any directory below a `.git` whose
    toplevel holds `phicad/bin/phicad`.  Nothing here calls `git init` on a path the finder
    did not approve, and `demos/` inside the fork is deliberately un-initialisable.

WHAT A MUTATION LEAVES BEHIND
    Two artefacts per agent action.  A commit whose subject is `phicad(<actor>): <message>`
    with a trailer block (`Phicad-Actor`, `Phicad-Tool`, `Phicad-Files`), so `git log` alone
    tells you who changed the board; and one JSON line in `.phicad/journal.jsonl` recording
    actor/action/arguments/files/commit/result.  The journal is gitignored on purpose: it is
    written on every action, and a tracked journal would make the autosnapshot watcher
    commit its own noise forever.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

CONFIG_DIR = ".phicad"
CONFIG_NAME = "project.json"
BRIDGE_NAME = "host-bridge.json"
JOURNAL_NAME = "journal.jsonl"
WATCH_NAME = "watch.json"
SCHEMA_VERSION = 1

#: Default committer when the project repo has no identity of its own.
DEFAULT_GIT_NAME = "PhiCad Agent"
DEFAULT_GIT_EMAIL = "phicad@localhost"

#: Documents whose *semantic* diff is worth extracting (see `diff()` / `extract()`).
KICAD_DOCS = (".kicad_pcb", ".kicad_sch")

# Where the fork's own `kicad-cli` sits inside a build tree, and which build tree this host
# uses by default, both live in phicad/pylib/build_layout.py (shared with the MCP server).
#: A `kicad-cli pcb diff` of two big boards is seconds, not minutes.
SEMANTIC_DIFF_TIMEOUT_S = 300.0
#: How many documents one `diff --semantic` will compare (a rename can touch many files).
MAX_SEMANTIC_DOCS = 8
#: Cap on the change list echoed back per document; the full report stays on disk.
MAX_CHANGES_ECHOED = 20
#: Suffixes that mark a directory as a KiCad project when walking up.
PROJECT_MARKERS = (".kicad_pro", ".kicad_pcb", ".kicad_sch")

#: How far up `find_project_root()` will look for a project marker in a standalone tree.
MAX_WALK_UP = 8

# Everything here is state KiCad or PhiCad regenerates, so committing it adds noise without
# adding recoverable design history.  `.history/` matters most: KiCad's own local-history
# feature mirrors the project there and keeps its OWN git repository inside it, so leaving it
# tracked would nest a repository in ours and commit a second copy of every save.  Keep this
# list in step with phicad/plugins/ai-view/project_history.py.
_GITIGNORE_BLOCK = """# PhiCad runtime and KiCad transient files
.phicad/host-bridge.json
.phicad/journal.jsonl
.phicad/watch.json
.history/
fp-info-cache
*.kicad_prl
*.lck
~*.kicad_*
*-backups/
_autosave-*
*.tmp
"""

# A KiCad design file is a 600 KB s-expression tree, and git's default text merge treats it
# as lines.  Two branches that touch one board therefore do not merely conflict -- git writes
# `<<<<<<<` INTO the file, and the result is no longer loadable by KiCad at all (measured:
# `Failed to load board: ... Got ''symbol'' ... line 136`).  The fork ships a structural
# 3-way merge driver for exactly this; `.gitattributes` is what routes paths to it.
#
# Mirrors common/git/git_init_handler.cpp:101-305, which does the same for repositories
# created from the GUI, so a project is configured identically however it was made.
_GITATTRIBUTES_HEADER = "# KiCad design files use custom merge drivers."
#: (glob, attribute) -- append-only; a glob that already carries any `merge=` is left alone.
_GITATTRIBUTES_ENTRIES = (
    ("*.kicad_pcb", "merge=kicad-pcb"),
    ("*.kicad_sch", "merge=kicad-sch"),
    ("*.kicad_sym", "merge=kicad-sym-lib"),
    ("*.kicad_mod", "merge=kicad-fp"),
)
#: (driver name, human name, --kind) for the four `merge.<driver>.*` config pairs.
_MERGE_DRIVERS = (
    ("kicad-pcb", "KiCad PCB merge driver", "pcb"),
    ("kicad-sch", "KiCad schematic merge driver", "sch"),
    ("kicad-sym-lib", "KiCad symbol library merge driver", "sym"),
    ("kicad-fp", "KiCad footprint merge driver", "fp"),
)

#: Argument keys whose values never reach the journal.  Substring match, case-insensitive.
_SECRET_KEY = re.compile(r"token|secret|key|password|passwd|credential|auth", re.I)
#: Journal values are truncated: an s-expression blob in an audit log helps nobody.
_MAX_JOURNAL_STR = 400
_MAX_JOURNAL_SEQ = 40

#: Unit separator / record separator, used to make `git log --format` machine-parsable.
_US = "\x1f"
_RS = "\x1e"

_TAG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$")


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

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "clean": self.clean, "branch": self.branch,
                "head": self.head, "detail": self.detail, "display": self.display()}


# --------------------------------------------------------------------------------------
# process plumbing
# --------------------------------------------------------------------------------------

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
               "HTTP_PROXY", "http_proxy")


def _git_env(strip_proxy: bool = False) -> Dict[str, str]:
    env = dict(os.environ)
    # An agent commit must never stop on an editor, a pager or a credential prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_EDITOR"] = "true"
    if strip_proxy:
        for var in _PROXY_VARS:
            env.pop(var, None)
    return env


def _run(cwd: Path, argv: Sequence[str], timeout: float = 20.0, check: bool = False,
         env: Optional[Dict[str, str]] = None):
    """Run one git command in `cwd`.  Raises HistoryError only on spawn/timeout or check."""
    try:
        proc = subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, env=env if env is not None else _git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HistoryError(f"could not run {' '.join(argv)}: {exc}") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise HistoryError(f"{' '.join(argv)}: {detail}")
    return proc


def _out(proc) -> str:
    return (proc.stdout or "").strip()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------
# The semantic half of a diff — the fork's own differ, shelled out to, never imported
# --------------------------------------------------------------------------------------
#
# This module stays stdlib-only and 3.9-safe (a KiCad plugin interpreter loads it), so the
# semantic answer cannot come from kipy or from the MCP venv.  It comes from the fork's
# `kicad-cli {pcb,sch} diff --format json`, run as a subprocess when the binary is
# resolvable and reported as skipped-with-a-reason when it is not.  That keeps
# `phicad project diff` and the `project_diff` MCP tool giving the SAME answer instead of
# the CLI printing two temp paths and calling it a diff.


def resolve_kicad_cli(explicit: Optional[Any] = None) -> Tuple[Optional[Path], str]:
    """(path, source) for the fork's `kicad-cli`, or (None, reason) when it is not built.

    Resolution order, matching `phicad/bin/phicad`'s `export_layout`: an explicit argument,
    `PHICAD_KICAD_CLI`, `PHICAD_BUILD`, then `<this checkout>/build/mac-dev`.  Never guesses
    a system-wide `kicad-cli`: a stable-KiCad binary would refuse fork-format files (and, on
    anything it could load, is exactly the version hazard the fork exists to avoid).
    """
    if explicit:
        path = Path(str(explicit)).expanduser()
        return (path, "argument") if path.exists() else (None, "kicad-cli not at {}".format(path))
    env_cli = os.environ.get("PHICAD_KICAD_CLI", "").strip()
    if env_cli:
        path = Path(env_cli).expanduser()
        return ((path, "PHICAD_KICAD_CLI") if path.exists()
                else (None, "PHICAD_KICAD_CLI points at a missing file: {}".format(path)))
    builds: List[Tuple[Path, str]] = []
    env_build = os.environ.get("PHICAD_BUILD", "").strip()
    if env_build:
        builds.append((Path(env_build).expanduser(), "PHICAD_BUILD"))
    # phicad/pylib/project_history.py -> <checkout>; build/<mac|linux>-dev per build_layout.
    import build_layout  # sibling module in phicad/pylib
    checkout = Path(__file__).resolve().parent.parent.parent
    builds.append((build_layout.default_build_dir(checkout), "default build tree"))
    for build, source in builds:
        for candidate in build_layout.kicad_cli_candidates(build):
            if candidate.exists():
                return candidate, source
    tried = ", ".join(str(b) for b, _ in builds)
    return None, "kicad-cli not built (looked under {})".format(tried)


def _cli_env() -> Dict[str, str]:
    """Env for the build-tree `kicad-cli`: it refuses to run without the build-dir flag."""
    env = _git_env(strip_proxy=True)
    env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
    return env


def _summarise_diff_report(payload: Any) -> Dict[str, Any]:
    """Change counts plus the first `MAX_CHANGES_ECHOED` changes of a `*_diff` JSON report."""
    out: Dict[str, Any] = {}
    if not isinstance(payload, dict):
        return out
    changes = payload.get("changes") or []
    kinds: Dict[str, int] = {}
    types: Dict[str, int] = {}
    items: List[Dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        kind = str(change.get("kind", "?"))
        type_name = str(change.get("typeName", "?"))
        kinds[kind] = kinds.get(kind, 0) + 1
        types[type_name] = types.get(type_name, 0) + 1
        if len(items) < MAX_CHANGES_ECHOED:
            items.append({
                "kind": kind,
                "type": type_name,
                "refdes": change.get("refdes"),
                "uuid": change.get("id"),
                "properties": [
                    {"name": prop.get("name"),
                     "before": (prop.get("before") or {}).get("v"),
                     "after": (prop.get("after") or {}).get("v")}
                    for prop in (change.get("properties") or []) if isinstance(prop, dict)
                ][:8],
            })
    out["doc_type"] = payload.get("docType")
    out["change_count"] = len(changes)
    out["changes_by_kind"] = kinds
    out["changes_by_type"] = types
    out["changes"] = items
    return out


def semantic_diff(doc: Dict[str, Any], kicad_cli: Any,
                  dest_dir: Optional[Any] = None) -> Dict[str, Any]:
    """Compare one document's two extracted revisions with the fork's own differ.

    `doc` is one entry of `ProjectHistory.diff()['documents']` (`{path, a, b}`).  Exit 0 is
    identical, exit **5** is "the documents differ" — a NORMAL answer, not a failure — and
    anything else is a real error reported in `error`.  Mirrors `tools_git._semantic_diff`
    so both front doors return the same keys.
    """
    cli = Path(str(kicad_cli))
    kind = "pcb" if str(doc.get("path", "")).endswith(".kicad_pcb") else "sch"
    left, right = Path(str(doc.get("a"))), Path(str(doc.get("b")))
    base = Path(dest_dir).expanduser() if dest_dir else Path(left).parent
    base.mkdir(parents=True, exist_ok=True)
    report = base / "{}--{}.{}diff.json".format(left.stem, right.stem, kind)[:200]
    argv = [str(cli), kind, "diff", "--format", "json", "-o", str(report),
            str(left), str(right)]
    out: Dict[str, Any] = {"path": doc.get("path"), "kind": kind,
                           "a": str(left), "b": str(right), "command": " ".join(argv)}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SEMANTIC_DIFF_TIMEOUT_S, env=_cli_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["ok"] = False
        out["error"] = "could not run {}: {}".format(cli.name, exc)
        return out
    out["exit_code"] = proc.returncode
    out["ok"] = proc.returncode in (0, 5)
    out["differs"] = proc.returncode == 5
    out["identical"] = proc.returncode == 0
    if not out["ok"]:
        out["error"] = ((proc.stderr or "").strip()[:400]
                        or "kicad-cli {} diff exit {}".format(kind, proc.returncode))
        return out
    payload: Any = None
    if report.exists():
        out["report_path"] = str(report)
        try:
            payload = json.loads(report.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            payload = None
    if payload is None and (proc.stdout or "").lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
    out.update(_summarise_diff_report(payload))
    return out


def attach_semantic(payload: Dict[str, Any], kicad_cli: Optional[Any] = None,
                    dest_dir: Optional[Any] = None) -> Dict[str, Any]:
    """Add the semantic half to a `ProjectHistory.diff()` result, in place, and return it.

    Adds `semantic` (one entry per compared document) and `semantic_change_count`, or
    `semantic_skipped` with the reason when there is nothing to compare or the fork's
    `kicad-cli` is not resolvable.  A missing binary DEGRADES the diff — the git stat and the
    extracted revisions are still returned — rather than failing it, because a source-only
    checkout can still answer "which files changed".
    """
    if not payload.get("ok"):
        return payload
    docs = [d for d in (payload.get("documents") or []) if isinstance(d, dict)]
    payload["semantic_requested"] = True
    if not docs:
        payload["semantic_skipped"] = \
            "no .kicad_pcb/.kicad_sch changed between these revisions"
        return payload
    cli, source = resolve_kicad_cli(kicad_cli)
    if cli is None:
        payload["semantic_skipped"] = source
        payload["hint"] = "phicad/bin/phicad build kicad-cli, or set PHICAD_KICAD_CLI"
        return payload
    payload["kicad_cli"] = str(cli)
    payload["kicad_cli_source"] = source
    results: List[Dict[str, Any]] = []
    for doc in docs[:MAX_SEMANTIC_DOCS]:
        if doc.get("error"):
            results.append({"path": doc.get("path"), "ok": False, "error": doc["error"]})
            continue
        results.append(semantic_diff(doc, cli, dest_dir=dest_dir))
    payload["semantic"] = results
    if len(docs) > MAX_SEMANTIC_DOCS:
        payload["semantic_truncated"] = len(docs) - MAX_SEMANTIC_DOCS
    payload["semantic_change_count"] = sum(int(r.get("change_count") or 0) for r in results)
    if not all(r.get("ok") for r in results):
        payload["ok"] = False
        payload["error"] = "one or more semantic diffs failed — see semantic[].error"
    return payload


# --------------------------------------------------------------------------------------
# Where is the project, and may we touch it?
# --------------------------------------------------------------------------------------


def _software_repo_toplevel(start: Path) -> Optional[Path]:
    """The nearest ancestor that is a git toplevel holding `phicad/bin/phicad`, if any.

    That marker means "this is the PhiCad source tree" (or a worktree of it).  Committing a
    board into it would mix hardware artefacts into the fork's own history, so every caller
    treats a hit here as a hard refusal.  `.git` may be a directory (normal clone) or a file
    (worktree/submodule); both are recognised.
    """
    for parent in [start] + list(start.parents):
        dot_git = parent / ".git"
        if dot_git.exists() and (parent / "phicad" / "bin" / "phicad").exists():
            return parent
    return None


def _has_marker(directory: Path) -> bool:
    try:
        return any(entry.is_file() and entry.suffix in PROJECT_MARKERS
                   for entry in directory.iterdir())
    except OSError:
        return False


def _path_start(path: Any) -> Tuple[Optional[Path], str]:
    """Resolve a caller's path to the directory whose project it is naming."""
    try:
        p = Path(path).expanduser()
    except (TypeError, ValueError):
        return None, "not a path"
    if not str(p).strip():
        return None, "empty path"
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
        # A board/project file that is about to be written: its directory is the project.
        return p.parent, ""
    if not p.exists():
        # A new directory is still the project named by the caller.  Git discovery below
        # runs from its existing parent so an enclosing checkout is not missed.
        return p, ""
    return None, f"path does not exist: {p}"


def _git_repository_root(start: Path) -> Optional[Path]:
    """Return the nearest valid Git toplevel containing ``start``.

    `git rev-parse` is intentional here rather than checking only ``start/.git``: a board
    below a monorepo is already version-controlled, and a worktree/submodule represents
    ``.git`` as a file.  Running from ``start`` also makes the nearest nested repository win
    over an enclosing one.
    """
    cwd = start if start.is_dir() else start.parent
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(cwd),
                              capture_output=True, text=True, timeout=10.0,
                              env=_git_env(strip_proxy=True))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        return Path(proc.stdout.strip()).resolve()
    except OSError:
        return None


def repository_root(path: Any) -> Optional[Path]:
    """Return the nearest existing Git repository containing ``path``, if any."""
    start, _ = _path_start(path)
    return _git_repository_root(start) if start is not None else None


def locate_project(path: Any) -> Tuple[Optional[Path], str]:
    """(project_root, reason), refusing paths whose repository scope is ambiguous.

    A path inside an existing Git repository is safe only when the named directory itself
    contains a KiCad marker.  That lets snapshots use the enclosing repository with a
    project-relative pathspec, without guessing which project an arbitrary checkout root
    means.  A standalone path retains the historical behaviour: walk up to its nearest
    marker and initialize that directory when requested.
    """
    start, error = _path_start(path)
    if start is None:
        return None, error

    home = Path.home().resolve()

    # The software-checkout refusal comes FIRST, before the generic "not one project" one.
    # Both refuse, so nothing unsafe happened either way -- but a caller who pointed at a
    # source checkout was told "pass the board or project directory", which invites them to
    # try harder at a path that can never work. "This is a software checkout, not a board"
    # names the actual mistake. The more specific refusal wins.
    #
    # It has to be here rather than after the repository branch, because a real checkout IS a
    # repository and has no KiCad marker, so the generic guard below always claimed it first
    # and this message was unreachable for exactly the case it describes.
    if (start / "phicad").is_dir():
        return None, f"{start} contains phicad/ — that is a software checkout, not a board"

    repo = _git_repository_root(start)
    if repo is not None:
        # A bare repository root or a random checkout directory can contain many boards;
        # accepting it would make `git add -A` an unsafe whole-repository operation.
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


def find_project_root(path: Any) -> Optional[Path]:
    """The directory that may hold this project's metadata, or None if refused."""
    return locate_project(path)[0]


def is_project_repo(path: Any) -> bool:
    """True when `path` resolves to a project whose nearest Git repository already exists."""
    root = find_project_root(path)
    return bool(root and repository_root(root))


# --------------------------------------------------------------------------------------
# Journal — append-only, gitignored, one JSON object per line
# --------------------------------------------------------------------------------------


def _redact(value: Any, depth: int = 0) -> Any:
    """Copy a JSON-ish value with secrets masked, long strings clipped, big lists cut."""
    if depth > 6:
        return "<deep>"
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            name = str(key)
            clean[name] = "***" if _SECRET_KEY.search(name) else _redact(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        items = [_redact(v, depth + 1) for v in list(value)[:_MAX_JOURNAL_SEQ]]
        if len(value) > _MAX_JOURNAL_SEQ:
            items.append(f"<+{len(value) - _MAX_JOURNAL_SEQ} more>")
        return items
    if isinstance(value, str):
        return value if len(value) <= _MAX_JOURNAL_STR else value[:_MAX_JOURNAL_STR] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    return _redact(str(value), depth)


class Journal:
    """`.phicad/journal.jsonl`: who did what to this project, in order, forever.

    Append-only and never rewritten, so a corrupted or hand-edited line costs one entry, not
    the log.  `tail()` skips unparsable lines rather than failing.  Writing is best-effort:
    an audit record must never be the reason a board edit fails, so `append()` swallows
    OSError and reports it in the returned dict.
    """

    def __init__(self, project_dir: Any):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.project_dir / CONFIG_DIR / JOURNAL_NAME

    def append(self, actor: str, action: str, args: Any = None, files: Any = None,
               commit: Optional[str] = None, result: Any = None,
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "ts": _now_iso(),
            "actor": str(actor or "unknown"),
            "action": str(action or "unknown"),
            "args": _redact(args if args is not None else {}),
            "files": [str(f) for f in (files or [])][:_MAX_JOURNAL_SEQ],
            "commit": commit,
            "result": _redact(result) if result is not None else None,
        }
        if extra:
            entry.update(_redact(extra))
        line = json.dumps(entry, sort_keys=True, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                return {"ok": False, "error": f"journal write failed: {exc}", "entry": entry}
        return {"ok": True, "entry": entry, "journal": str(self.path)}

    def tail(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        rows: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return rows[-max(1, int(limit)):] if rows else []

    def count(self) -> int:
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0


def journal_tail(project_dir: Any, limit: int = 50) -> List[Dict[str, Any]]:
    """The last `limit` journal entries of a project (empty list when there is no journal)."""
    return Journal(project_dir).tail(limit)


# --------------------------------------------------------------------------------------
# Snapshot results — a dict (JSON-serialisable, rich) that is falsy when nothing happened
# --------------------------------------------------------------------------------------


class SnapshotResult(dict):
    """The outcome of a commit attempt.

    A dict so tools can return it verbatim, but `bool(result)` is `committed`, which is what
    the ai-view plugin and its tests assert on (`assertFalse(history.autosnapshot(...))`).
    An ordinary non-empty dict is always truthy, and that would have silently inverted the
    meaning of every existing call site.
    """

    __slots__ = ()

    def __bool__(self) -> bool:               # noqa: D105 - see class docstring
        return bool(self.get("committed"))


def _no_commit(reason: str, **extra: Any) -> SnapshotResult:
    res = SnapshotResult(ok=True, committed=False, commit=None, reason=reason,
                         changed_files=[], insertions=0, deletions=0)
    res.update(extra)
    return res


def _trailer_key(raw: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(raw)) if p]
    return "Phicad-" + "-".join(p[:1].upper() + p[1:] for p in parts) if parts else ""


class ProjectHistory:
    """Initialize, snapshot, inspect and rewind one board-project Git scope.

    The scope may be a standalone repository or a subdirectory of an enclosing repository.
    Every method is safe to call concurrently from a GUI thread and a poll thread: git calls
    are serialised on one lock per instance.  Nothing here ever touches a repository the
    finder refused (see `locate_project`), and nothing rewrites published history — `undo`
    is `git revert`, not `git reset`.
    """

    def __init__(self, project_dir: Any, enforce_safety: bool = True):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self._lock = threading.Lock()
        self._observed = ""
        self._stable_since = 0.0
        self._enforce_safety = bool(enforce_safety)
        self._created_repository = False
        self.journal = Journal(self.project_dir)
        #: What the last `ensure()` did about the merge driver; {} when it has not run.
        self.last_merge_driver: Dict[str, Any] = {}

    # -- layout ------------------------------------------------------------------------

    @property
    def metadata_dir(self) -> Path:
        return self.project_dir / CONFIG_DIR

    @property
    def config_path(self) -> Path:
        return self.metadata_dir / CONFIG_NAME

    @property
    def journal_path(self) -> Path:
        return self.journal.path

    @property
    def watch_state_path(self) -> Path:
        return self.metadata_dir / WATCH_NAME

    @property
    def repository_dir(self) -> Optional[Path]:
        """The nearest existing Git toplevel, including an enclosing monorepo."""
        return repository_root(self.project_dir)

    @property
    def uses_enclosing_repository(self) -> bool:
        """Whether this project is scoped inside a repository rooted above it."""
        repo = self.repository_dir
        return bool(repo and repo != self.project_dir)

    @property
    def is_repository(self) -> bool:
        """True when this project already has Git history, local or enclosing."""
        return self.repository_dir is not None

    def _guard(self) -> None:
        """Refuse unsafe paths, including an existing software checkout."""
        if not self._enforce_safety:
            return
        root, reason = locate_project(self.project_dir)
        if root is None:
            raise HistoryError(reason)

    def _git(self, argv: Sequence[str], timeout: float = 20.0, check: bool = False,
             strip_proxy: bool = False, env: Optional[Dict[str, str]] = None):
        return _run(self.project_dir, ["git"] + list(argv), timeout=timeout, check=check,
                    env=env if env is not None else _git_env(strip_proxy=strip_proxy))

    def _commit_env(self) -> Dict[str, str]:
        """Supply a fallback identity without rewriting an enclosing repo's config."""
        env = _git_env()
        if not _out(self._git(["config", "user.name"])):
            env["GIT_AUTHOR_NAME"] = DEFAULT_GIT_NAME
            env["GIT_COMMITTER_NAME"] = DEFAULT_GIT_NAME
        if not _out(self._git(["config", "user.email"])):
            env["GIT_AUTHOR_EMAIL"] = DEFAULT_GIT_EMAIL
            env["GIT_COMMITTER_EMAIL"] = DEFAULT_GIT_EMAIL
        return env

    # -- creation ----------------------------------------------------------------------

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

    def _write_gitattributes(self) -> List[str]:
        """Route KiCad design files to the structural merge driver.  Returns globs added.

        Append-only, and per glob: a line whose glob already carries ANY `merge=` value is
        left exactly as it is, because that is a choice someone made -- possibly to opt out.
        """
        path = self.project_dir / ".gitattributes"
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        configured = set()
        for line in previous.splitlines():
            bare = line.split("#", 1)[0].split()
            if len(bare) >= 2 and any(tok.startswith("merge=") for tok in bare[1:]):
                configured.add(bare[0])
        missing = [(glob, attr) for glob, attr in _GITATTRIBUTES_ENTRIES
                   if glob not in configured]
        if not missing:
            return []
        chunk = ""
        if not previous:
            chunk += _GITATTRIBUTES_HEADER + "\n"
        elif not previous.endswith("\n"):
            chunk = "\n"
        chunk += "".join("{} {}\n".format(glob, attr) for glob, attr in missing)
        path.write_text(previous + chunk, encoding="utf-8")
        return [glob for glob, _ in missing]

    def _merge_driver_config(self, cli: Path) -> List[str]:
        """Set the `merge.kicad-*` / `mergetool.kicad` keys that are missing.  Returns keys set.

        Checked with `--local`: custom driver commands and a user's merge tool are preserved.
        Missing keys are installed; exact legacy generated commands may be repaired.

        Flat Linux build-tree binaries need the build-dir flag to locate sibling kifaces.
        Installed binaries and macOS bundles retain their normal resource lookup. Only an
        exact previously generated command is migrated; custom driver commands stay intact.
        """
        quoted = '"{}"'.format(cli)
        wanted = []
        for driver, human, kind in _MERGE_DRIVERS:
            wanted.append(("merge.{}.name".format(driver), human))
            wanted.append(("merge.{}.driver".format(driver),
                           '{} git-mergedriver --kind {} "%O" "%A" "%B" --output "%A"'
                           .format(quoted, kind)))
        wanted.append(("mergetool.kicad.cmd",
                       '{} mergetool "$BASE" "$LOCAL" "$REMOTE" --output "$MERGED"'
                       .format(quoted)))
        wanted.append(("mergetool.kicad.trustExitCode", "true"))
        wanted.append(("merge.tool", "kicad"))
        legacy = dict(wanted)
        build = cli.parent.parent
        if (cli.parent.name == "kicad" and (build / "CMakeCache.txt").is_file()
                and (build / "pcbnew").is_dir()):
            wanted = [(key, "env KICAD_RUN_FROM_BUILD_DIR=1 " + value
                       if key.endswith(".driver") or key == "mergetool.kicad.cmd" else value)
                      for key, value in wanted]
        written: List[str] = []
        for key, value in wanted:
            got = self._git(["config", "--local", "--get", key])
            if got.returncode == 0 and _out(got):
                if _out(got) != legacy[key] or value == legacy[key]:
                    continue
            if self._git(["config", "--local", key, value]).returncode == 0:
                written.append(key)
        return written

    def install_merge_driver(self) -> Dict[str, Any]:
        """Make this repository safe for a `git merge` that touches KiCad files.

        Two halves, deliberately independent: `.gitattributes` is committed and travels with
        the project, while the `merge.*` config is per-clone and cannot.  So the attributes
        are always written -- a clone on a machine with a built fork then only needs the
        config -- and a missing `kicad-cli` skips ONLY the config half, with a reason.

        Never raises and never fails project creation: a repository with attributes and no
        driver behaves exactly like today's (git falls back to text merge), which is the
        status quo, not a regression.
        """
        result: Dict[str, Any] = {"installed": False, "gitattributes_added": [],
                                 "config_keys": [], "cli": None, "cli_source": ""}
        # Refuse a software checkout explicitly.  `_guard()` cannot do this job: it returns
        # early for a directory that is ALREADY a repository, and the PhiCad source tree is
        # one -- so a direct call here would append the four globs to the fork's own
        # upstream-tracked .gitattributes and create a permanent upstream merge conflict.
        # ensure() is guarded, but this method is public and callable on its own.
        if self._enforce_safety:
            root, reason = locate_project(self.project_dir)
            if root is None:
                result["reason"] = reason
                return result
        try:
            result["gitattributes_added"] = self._write_gitattributes()
        except OSError as exc:
            result["reason"] = "could not write .gitattributes: {}".format(exc)
            return result
        if not self.is_repository:
            result["reason"] = "not a git repository yet"
            return result
        cli, source = resolve_kicad_cli()
        if cli is None:
            result["reason"] = source
            result["hint"] = "phicad/bin/phicad build kicad-cli, or set PHICAD_KICAD_CLI"
            return result
        result["cli"] = str(cli)
        result["cli_source"] = source
        try:
            result["config_keys"] = self._merge_driver_config(cli)
        except Exception as exc:                                       # noqa: BLE001
            result["reason"] = "{}: {}".format(type(exc).__name__, exc)
            return result
        result["installed"] = True
        return result

    def _init_git(self) -> None:
        # `is_repository` includes a parent repo.  Never run `git init` merely because the
        # board directory itself lacks `.git`.
        if self.is_repository:
            return
        self._guard()
        proc = self._git(["init", "-b", "main"])
        if proc.returncode != 0:
            # Git <2.28 has no `-b`.  PhiCad currently ships on newer Git, but the
            # fallback costs little and keeps imported projects portable.
            self._git(["init"], check=True)
            self._git(["checkout", "-b", "main"], check=True)
        self._created_repository = True

    def ensure_identity(self) -> Dict[str, str]:
        """Give a newly-created standalone repo a committer without rewriting a monorepo.

        An enclosing repository belongs to the user, not to PhiCad.  Its local identity is
        therefore never changed; `_commit_env()` supplies a process-local fallback only when
        Git has no identity at all.
        """
        want_name = os.environ.get("PHICAD_GIT_NAME", "").strip() or DEFAULT_GIT_NAME
        want_email = os.environ.get("PHICAD_GIT_EMAIL", "").strip() or DEFAULT_GIT_EMAIL
        if self._created_repository:
            got = self._git(["config", "--local", "user.name"])
            if got.returncode != 0 or not _out(got):
                self._git(["config", "--local", "user.name", want_name], check=True)
            got_email = self._git(["config", "--local", "user.email"])
            if got_email.returncode != 0 or not _out(got_email):
                self._git(["config", "--local", "user.email", want_email], check=True)
        return {"name": _out(self._git(["config", "user.name"])) or want_name,
                "email": _out(self._git(["config", "user.email"])) or want_email}

    # Kept for the ai-view plugin's original private call site.
    _ensure_identity = ensure_identity

    def import_design(self, design_path: Any) -> Optional[Path]:
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

    def ensure(self, design_path: Optional[Any] = None,
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
            self.ensure_identity()
            # After _init_git: the config half needs a repository to write --local keys into.
            # Before the baseline commit: .gitattributes should be IN it, not a later change.
            self.last_merge_driver = self.install_merge_driver()
            self._snapshot_unlocked(message, actor="init")
            return self._status_unlocked()

    # -- commit ------------------------------------------------------------------------

    def _porcelain(self) -> str:
        """`git status --porcelain`, minus PhiCad's own runtime files.

        The watcher's stability fingerprint is derived from this, and the watcher writes
        `.phicad/watch.json` on every poll.  If that file could appear here, each poll would
        change the fingerprint the next poll compares against and a save would never look
        settled -- so it is filtered out here as well as gitignored.
        """
        if not self.is_repository:
            return ""
        proc = self._git(["status", "--porcelain=v1", "--untracked-files=all", "--", "."],
                         check=True)
        keep = [ln for ln in (proc.stdout or "").splitlines()
                if CONFIG_DIR + "/" + WATCH_NAME not in ln]
        return "".join(ln + "\n" for ln in keep)

    def _commit_message(self, message: str, actor: str, files: Optional[Sequence[str]],
                        meta: Optional[Dict[str, Any]]) -> str:
        subject = (message or "snapshot project changes").strip().splitlines()[0]
        if not re.match(r"^phicad\s*\(", subject):
            subject = re.sub(r"^phicad:\s*", "", subject)
            subject = f"phicad({actor}): {subject}"
        trailers = ["Phicad-Actor: " + actor,
                    "Phicad-Tool: " + str((meta or {}).get("tool") or actor),
                    "Phicad-Files: " + str(len(files) if files else 0)]
        for key, value in sorted((meta or {}).items()):
            if key == "tool" or value is None or isinstance(value, (dict, list, tuple)):
                continue
            name = _trailer_key(key)
            if name and name not in ("Phicad-Actor", "Phicad-Tool", "Phicad-Files"):
                trailers.append("{}: {}".format(name, str(value).splitlines()[0][:120]))
        body = (message or "").strip()
        extra = "\n".join(body.splitlines()[1:]).strip()
        return subject + "\n\n" + (extra + "\n\n" if extra else "") + "\n".join(trailers) + "\n"

    def _stat_head(self) -> Dict[str, Any]:
        """{commit, changed_files, insertions, deletions} for HEAD, from `git show`."""
        sha = _out(self._git(["rev-parse", "HEAD"]))
        proc = self._git(["show", "--numstat", "--format=", "HEAD", "--", "."])
        files: List[str] = []
        insertions = deletions = 0
        for line in (proc.stdout or "").splitlines():
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 3:
                continue
            add, rem, name = parts
            files.append(name)
            insertions += int(add) if add.isdigit() else 0
            deletions += int(rem) if rem.isdigit() else 0
        stat = _out(self._git(["show", "--stat", "--format=%h %s", "HEAD", "--", "."]))
        return {"commit": sha, "short": sha[:10], "changed_files": files,
                "insertions": insertions, "deletions": deletions, "stat": stat}

    def _snapshot_unlocked(self, message: str, files: Optional[Sequence[str]] = None,
                           actor: str = "agent",
                           meta: Optional[Dict[str, Any]] = None) -> SnapshotResult:
        if not self.is_repository:
            raise HistoryError(f"{self.project_dir} is not a PhiCad project repository")
        rel = self._relativise(files) if files else None
        if files and not rel:
            # Every named path was outside the repository.  Falling through to the "commit
            # everything" branch here would turn a narrow request into a whole-project
            # commit -- the caller asked for specific files and got something else.
            return _no_commit("none of the listed files are inside the project repository",
                              requested=[str(f) for f in files])
        if rel:
            self._git(["add", "-A", "--"] + rel, check=True)
            staged = _out(self._git(["diff", "--cached", "--name-only", "--"] + rel))
            if not staged:
                return _no_commit("listed files are unchanged", files=rel)
        else:
            if not self._porcelain().strip():
                return _no_commit("working tree is clean")
            self._git(["add", "-A", "--", "."], check=True)
            if not _out(self._git(["diff", "--cached", "--name-only", "--", "."])):
                return _no_commit("nothing staged (all changes are ignored)")

        text = self._commit_message(message, actor, rel, meta)
        argv = ["commit", "--no-gpg-sign", "-m", text, "--only", "--"]
        # Always constrain commits to this project.  In an enclosing repository, an
        # unrelated staged change must never hitch a ride on an Agent View snapshot.
        argv += rel or ["."]
        proc = self._git(argv, timeout=60.0, env=self._commit_env())
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "commit failed").strip()
            raise HistoryError(detail)
        self._observed = ""
        self._stable_since = 0.0
        res = SnapshotResult(ok=True, committed=True, actor=actor,
                             message=text.splitlines()[0])
        res.update(self._stat_head())
        return res

    def _relativise(self, files: Sequence[Any]) -> List[str]:
        """Project-relative POSIX paths for `git add`, dropping anything outside the repo."""
        rel: List[str] = []
        for item in files or []:
            p = Path(str(item)).expanduser()
            if not p.is_absolute():
                p = self.project_dir / p
            try:
                resolved = p.resolve()
            except OSError:
                continue
            try:
                rel.append(resolved.relative_to(self.project_dir).as_posix())
            except ValueError:
                continue
        # De-duplicate, keep order.
        seen: Dict[str, None] = {}
        for item in rel:
            seen.setdefault(item, None)
        return list(seen.keys())

    def snapshot(self, message: str = "phicad: snapshot project changes",
                 files: Optional[Sequence[Any]] = None, actor: Optional[str] = None,
                 meta: Optional[Dict[str, Any]] = None) -> SnapshotResult:
        """Commit the project (all changes, or only `files`) as one auditable revision.

        Returns a SnapshotResult: falsy with `reason` when there was nothing to commit,
        otherwise `{commit, short, changed_files, insertions, deletions, stat}`.
        """
        with self._lock:
            return self._snapshot_unlocked(message, files=files, actor=actor or "agent",
                                           meta=meta)

    def _load_watch(self) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(self.watch_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _save_watch(self, stamp: str) -> None:
        try:
            _atomic_json(self.watch_state_path,
                         {"fingerprint": stamp, "wall": time.time()})
        except OSError:
            pass

    def _clear_watch(self) -> None:
        self._observed = ""
        self._stable_since = 0.0
        try:
            self.watch_state_path.unlink()
        except OSError:
            pass

    def autosnapshot(self, stable_seconds: float = 2.0,
                     now: Optional[float] = None) -> SnapshotResult:
        """Commit once a dirty working tree has remained unchanged for a short window.

        Callers poll this.  A KiCad save can replace several files in quick succession;
        waiting for a stable porcelain result prevents half-save commits.

        The window survives the caller.  A long-lived poll thread (the ai-view plugin) keeps
        it in memory, but an MCP tool is a NEW object on every call, so a purely in-memory
        window would restart on each poll and a stateless caller could never commit at all.
        The fingerprint and a wall-clock stamp are therefore mirrored into
        `.phicad/watch.json` (gitignored, and filtered out of the fingerprint itself).
        Passing `now` selects pure in-memory behaviour, which is what makes the window
        testable with an injected clock.
        """
        with self._lock:
            if not self.is_repository:
                return _no_commit("not a project repository")
            stamp = self._porcelain()
            if not stamp.strip():
                self._clear_watch()
                return _no_commit("working tree is clean")
            clock = time.monotonic() if now is None else now
            if stamp != self._observed:
                waited = 0.0
                persisted = self._load_watch() if now is None else None
                if persisted and persisted.get("fingerprint") == stamp:
                    # Another poll (possibly another process) already saw this exact state.
                    waited = max(0.0, time.time() - float(persisted.get("wall") or 0.0))
                else:
                    self._save_watch(stamp)
                self._observed = stamp
                self._stable_since = clock - waited
                if waited < stable_seconds:
                    return _no_commit(
                        "changes still arriving" if waited == 0.0
                        else "waiting for the save to settle",
                        stable_for_s=round(waited, 2), pending=stamp.splitlines()[:20])
            else:
                waited = clock - self._stable_since
                if waited < stable_seconds:
                    return _no_commit("waiting for the save to settle",
                                      stable_for_s=round(waited, 2),
                                      pending=stamp.splitlines()[:20])
            when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            result = self._snapshot_unlocked(f"autosnapshot {when}", actor="editor",
                                             meta={"tool": "autosnapshot"})
            self._clear_watch()
            return result

    def watch_once(self, stable_seconds: float = 2.0,
                   now: Optional[float] = None) -> SnapshotResult:
        """One iteration of a poll loop: autosnapshot when the working tree has settled."""
        return self.autosnapshot(stable_seconds=stable_seconds, now=now)

    # -- status ------------------------------------------------------------------------

    def _status_unlocked(self) -> HistoryStatus:
        if not self.is_repository:
            return HistoryStatus(False, True, detail="Git not initialized")
        dirty = bool(self._porcelain().strip())
        branch = _out(self._git(["branch", "--show-current"])) or "detached"
        head_proc = self._git(["rev-parse", "--short", "HEAD"])
        head = _out(head_proc) if head_proc.returncode == 0 else ""
        return HistoryStatus(True, not dirty, branch=branch, head=head)

    def status(self) -> HistoryStatus:
        with self._lock:
            try:
                return self._status_unlocked()
            except HistoryError as exc:
                return HistoryStatus(False, True, detail=str(exc))

    def status_dict(self) -> Dict[str, Any]:
        """`status()` plus the pending file list, the commit count and the journal size."""
        with self._lock:
            try:
                base = self._status_unlocked()
            except HistoryError as exc:
                return {"ok": False, "error": str(exc), "project_dir": str(self.project_dir)}
            out: Dict[str, Any] = {"ok": True, "project_dir": str(self.project_dir),
                                   "repository_dir": (str(self.repository_dir)
                                                       if self.repository_dir else None),
                                   "uses_enclosing_repository": self.uses_enclosing_repository}
            out.update(base.to_dict())
            if not base.enabled:
                out["pending"] = []
                out["commits"] = 0
                return out
            pending = []
            for line in self._porcelain().splitlines():
                if len(line) > 3:
                    pending.append({"state": line[:2].strip(), "path": line[3:]})
            count = _out(self._git(["rev-list", "--count", "HEAD"]))
            out["pending"] = pending
            out["commits"] = int(count) if count.isdigit() else 0
            out["journal_entries"] = self.journal.count()
            out["identity"] = {
                "name": _out(self._git(["config", "user.name"])),
                "email": _out(self._git(["config", "user.email"])),
            }
            return out

    # -- history / diff ----------------------------------------------------------------

    def history(self, limit: int = 20, path: Optional[Any] = None) -> List[Dict[str, Any]]:
        """The last `limit` commits as {sha, short, ts, iso, subject, actor, tool, files}.

        `path` restricts the log to one file (project-relative or absolute).  Parsed with
        ASCII record/unit separators instead of a delimiter that can appear in a subject.
        """
        with self._lock:
            if not self.is_repository:
                return []
            fmt = _RS + _US.join(["%H", "%ct", "%s", "%an",
                                  "%(trailers:key=Phicad-Actor,valueonly)",
                                  "%(trailers:key=Phicad-Tool,valueonly)"]) + _US
            argv = ["log", "-n", str(max(1, int(limit))), "--format=" + fmt, "--name-only"]
            rel = self._relativise([path]) if path else []
            argv += ["--follow"] if rel else []
            # History from a monorepo is project history, not the entire software checkout.
            argv += ["--"] + (rel or ["."])
            proc = self._git(argv, timeout=30.0)
            if proc.returncode != 0:
                return []
            rows: List[Dict[str, Any]] = []
            for chunk in (proc.stdout or "").split(_RS):
                if not chunk.strip():
                    continue
                head, _, tail = chunk.partition(_US + "\n")
                fields = (head if _US in head else chunk).split(_US)
                if len(fields) < 6:
                    continue
                sha, ts, subject, author, actor, tool = fields[:6]
                files = [ln for ln in tail.splitlines() if ln.strip()]
                if not files:
                    files = [ln for ln in _US.join(fields[6:]).splitlines() if ln.strip()]
                rows.append({
                    "sha": sha.strip(),
                    "short": sha.strip()[:10],
                    "ts": int(ts) if ts.strip().isdigit() else None,
                    "iso": (datetime.fromtimestamp(int(ts), timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ")) if ts.strip().isdigit() else "",
                    "subject": subject.strip(),
                    "author": author.strip(),
                    "actor": actor.strip() or None,
                    "tool": tool.strip() or None,
                    "files": files,
                })
            return rows

    def extract(self, rev: str, relpath: Any, dest_dir: Optional[Any] = None) -> Path:
        """Write `rev:relpath` to a temp file keeping its suffix; return the path.

        This is what makes a *semantic* diff possible: `kicad-cli pcb diff` compares two
        files on disk, and a historical revision is not on disk.  The suffix is preserved
        because the CLI dispatches on it.
        """
        rel = self._relativise([relpath])
        target = rel[0] if rel else str(relpath)
        proc = self._git(["show", "{}:{}".format(rev, target)], timeout=60.0)
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            raise HistoryError(f"git show {rev}:{target}: {detail}")
        suffix = Path(target).suffix
        stem = Path(target).stem
        safe_rev = re.sub(r"[^A-Za-z0-9]+", "-", str(rev)).strip("-") or "rev"
        base = Path(dest_dir).expanduser() if dest_dir else Path(
            tempfile.mkdtemp(prefix="phicad-hist-"))
        base.mkdir(parents=True, exist_ok=True)
        out = base / "{}.{}{}".format(stem, safe_rev, suffix)
        out.write_text(proc.stdout, encoding="utf-8")
        return out

    def diff(self, rev_a: str = "HEAD~1", rev_b: str = "HEAD",
             path: Optional[Any] = None, extract_docs: bool = True,
             dest_dir: Optional[Any] = None) -> Dict[str, Any]:
        """Textual diff between two revisions, plus extracted copies of the KiCad documents.

        Returns `stat` (`git diff --stat`), per-file `files` entries
        (`{path, status, insertions, deletions}`) and, for each changed `.kicad_pcb` /
        `.kicad_sch` that exists in both revisions, `documents[i] = {path, a, b}` with `a`/`b`
        pointing at temp files.  Feed those two paths to `kicad-cli pcb diff` for the
        semantic answer — the text diff of an s-expression board is close to unreadable.
        `rev_b` may be the literal string "WORKTREE" to diff against uncommitted changes.
        """
        with self._lock:
            if not self.is_repository:
                return {"ok": False, "error": "not a project repository",
                        "project_dir": str(self.project_dir)}
            rel = self._relativise([path]) if path else []
            worktree = str(rev_b).upper() in ("WORKTREE", "WORKING", "", "NONE")
            revs = [rev_a] if worktree else [rev_a, rev_b]
            for rev in revs:
                if self._git(["rev-parse", "--verify", "--quiet",
                              str(rev) + "^{commit}"]).returncode != 0:
                    return {"ok": False, "error": f"unknown revision: {rev}"}
            # An omitted path means this project subtree, never the whole enclosing repo.
            pathspec = ["--"] + (rel or ["."])
            stat = _out(self._git(["diff", "--stat"] + revs + pathspec, timeout=60.0))
            numstat = self._git(["diff", "--numstat"] + revs + pathspec, timeout=60.0)
            names = self._git(["diff", "--name-status"] + revs + pathspec, timeout=60.0)
            status_by_path: Dict[str, str] = {}
            for line in (names.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    status_by_path[parts[-1]] = parts[0]
            files: List[Dict[str, Any]] = []
            for line in (numstat.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                add, rem, name = parts
                files.append({
                    "path": name,
                    "status": status_by_path.get(name, "M"),
                    "insertions": int(add) if add.isdigit() else None,
                    "deletions": int(rem) if rem.isdigit() else None,
                    "binary": not add.isdigit(),
                })
            docs: List[Dict[str, Any]] = []
            if extract_docs:
                base = Path(dest_dir).expanduser() if dest_dir else Path(
                    tempfile.mkdtemp(prefix="phicad-diff-"))
                for entry in files:
                    name = entry["path"]
                    if Path(name).suffix not in KICAD_DOCS or entry["status"] == "A":
                        continue
                    try:
                        left = self.extract(rev_a, name, dest_dir=base)
                        right = (self.project_dir / name) if worktree else \
                            self.extract(rev_b, name, dest_dir=base)
                    except HistoryError as exc:
                        docs.append({"path": name, "error": str(exc)})
                        continue
                    if not Path(right).exists():
                        docs.append({"path": name, "error": "file missing in rev_b"})
                        continue
                    docs.append({"path": name, "a": str(left), "b": str(right)})
            return {"ok": True, "project_dir": str(self.project_dir),
                    "rev_a": rev_a, "rev_b": "WORKTREE" if worktree else rev_b,
                    "stat": stat, "files": files, "documents": docs,
                    "changed": len(files)}

    # -- rewind ------------------------------------------------------------------------

    def restore(self, rev: str, paths: Optional[Sequence[Any]] = None,
                dry_run: bool = True) -> Dict[str, Any]:
        """Bring files back from `rev` into the working tree (`git checkout rev -- paths`).

        `dry_run=True` (the default) only reports which files differ between the working
        tree and `rev`; nothing is written.  With `dry_run=False` the files are restored AND
        committed, so the recovery itself is part of the audit trail — a bare checkout would
        leave the project dirty and let the autosnapshot watcher describe it as an edit.
        An apply is refused when any project file is dirty, and a path list must stay wholly
        inside the project rather than being silently narrowed.  History is never rewritten.
        """
        with self._lock:
            if not self.is_repository:
                return {"ok": False, "error": "not a project repository"}
            if self._git(["rev-parse", "--verify", "--quiet",
                          str(rev) + "^{commit}"]).returncode != 0:
                return {"ok": False, "error": f"unknown revision: {rev}"}
            requested = list(paths) if paths is not None else []
            rel = self._relativise(requested) if requested else []
            if requested:
                outside = [str(item) for item in requested
                           if not self._relativise([item])]
                if outside:
                    return {"ok": False,
                            "error": "requested path(s) are outside the project: "
                                     + ", ".join(outside),
                            "paths": [str(item) for item in requested]}
            spec = (["--"] + rel) if rel else ["--", "."]
            probe = self._git(["diff", "--numstat", rev] + spec, timeout=60.0)
            would: List[Dict[str, Any]] = []
            for line in (probe.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    would.append({"path": parts[2],
                                  "insertions": parts[1] if parts[1].isdigit() else None,
                                  "deletions": parts[0] if parts[0].isdigit() else None})
            out: Dict[str, Any] = {"ok": True, "rev": rev, "dry_run": bool(dry_run),
                                   "paths": rel or ["<all>"],
                                   "would_change": [w["path"] for w in would],
                                   "detail": would}
            if dry_run:
                out["restored"] = False
                return out
            if not would:
                out["restored"] = False
                out["reason"] = "working tree already matches that revision"
                out["snapshot"] = _no_commit("nothing to restore")
                return out
            pending = self._porcelain().splitlines()
            if pending:
                return {"ok": False, "error": "working tree is dirty — snapshot first",
                        "pending": pending[:20]}
            checkout = self._git(["checkout", rev] + spec, timeout=60.0)
            if checkout.returncode != 0:
                return {"ok": False, "error": (checkout.stderr or "checkout failed").strip(),
                        "rev": rev, "paths": rel}
            short = _out(self._git(["rev-parse", "--short", rev]))
            snap = self._snapshot_unlocked(
                "restore {} from {}".format(", ".join(rel) if rel else "project", short),
                files=[w["path"] for w in would], actor="restore",
                meta={"tool": "project_restore", "source-rev": short})
            out["restored"] = True
            out["snapshot"] = snap
            return out

    def undo_last(self, dry_run: bool = True) -> Dict[str, Any]:
        """Undo the most recent commit with `git revert --no-edit` (history is preserved).

        A revert adds a commit that inverts HEAD; it never drops a revision, so an undo can
        itself be undone.  `dry_run=True` reports what HEAD changed (that is exactly what a
        revert would put back).  A dirty working tree is refused rather than merged into the
        revert, because that would attribute the designer's unsaved edits to the undo.
        """
        with self._lock:
            if not self.is_repository:
                return {"ok": False, "error": "not a project repository"}
            if self.uses_enclosing_repository:
                return {"ok": False,
                        "error": "cannot undo a project commit inside an enclosing repository",
                        "hint": "revert the enclosing repository commit explicitly"}
            head = _out(self._git(["rev-parse", "HEAD"]))
            subject = _out(self._git(["log", "-1", "--format=%s"]))
            stat = self._stat_head()
            out: Dict[str, Any] = {"ok": True, "dry_run": bool(dry_run), "head": head,
                                   "head_short": head[:10], "head_subject": subject,
                                   "would_restore": stat["changed_files"]}
            if _out(self._git(["rev-list", "--count", "HEAD"])) == "1":
                out["ok"] = False
                out["error"] = "HEAD is the initial commit; there is nothing to revert onto"
                return out
            if dry_run:
                out["reverted"] = False
                return out
            if self._porcelain().strip():
                return {"ok": False, "error": "working tree is dirty — snapshot first",
                        "pending": self._porcelain().splitlines()[:20]}
            proc = self._git(["revert", "--no-edit", "--no-gpg-sign", head], timeout=60.0)
            if proc.returncode != 0:
                self._git(["revert", "--abort"])
                return {"ok": False, "error": (proc.stderr or "revert failed").strip(),
                        "head": head}
            out["reverted"] = True
            out["revert_commit"] = self._stat_head()
            return out

    def tag(self, name: str, message: str = "") -> Dict[str, Any]:
        """Mark the current revision as a milestone (an annotated tag, e.g. drc-clean-v1)."""
        with self._lock:
            if not self.is_repository:
                return {"ok": False, "error": "not a project repository"}
            if self.uses_enclosing_repository:
                return {"ok": False,
                        "error": "cannot tag a project inside an enclosing repository",
                        "hint": "tag the enclosing repository explicitly"}
            if not _TAG_NAME.match(str(name or "")):
                return {"ok": False, "error": f"invalid tag name: {name!r}",
                        "hint": "letters, digits and . _ - / only"}
            if self._git(["rev-parse", "--verify", "--quiet",
                          "refs/tags/" + name]).returncode == 0:
                return {"ok": False, "error": f"tag already exists: {name}"}
            self.ensure_identity()
            proc = self._git(["tag", "-a", name, "-m", message or f"phicad milestone {name}"],
                             timeout=30.0)
            if proc.returncode != 0:
                return {"ok": False, "error": (proc.stderr or "tag failed").strip()}
            head = _out(self._git(["rev-parse", "HEAD"]))
            return {"ok": True, "tag": name, "commit": head, "short": head[:10],
                    "message": message or f"phicad milestone {name}",
                    "tags": _out(self._git(["tag", "--list"])).split()}

    def tags(self) -> List[Dict[str, Any]]:
        """Every tag with the COMMIT it points at, newest first.

        `%(objectname)` on an annotated tag is the sha of the tag *object*, not of the commit
        -- which reads as a plausible sha that matches nothing in `git log`.  `%(*objectname)`
        is the dereferenced commit and is empty for a lightweight tag, so prefer it and fall
        back.
        """
        with self._lock:
            if not self.is_repository:
                return []
            proc = self._git(["for-each-ref", "--sort=-creatordate",
                              "--format=%(refname:short)" + _US + "%(objectname)" + _US
                              + "%(*objectname)" + _US + "%(contents:subject)", "refs/tags"])
            rows = []
            for line in (proc.stdout or "").splitlines():
                parts = line.split(_US)
                if len(parts) >= 3:
                    rows.append({"tag": parts[0], "commit": parts[2] or parts[1],
                                 "tag_object": parts[1],
                                 "message": parts[3] if len(parts) > 3 else ""})
            return rows

    # -- remote (optional; a project repo usually has none) -----------------------------

    def remote_status(self) -> Dict[str, Any]:
        """Ahead/behind against the tracking branch, without touching the network.

        No remote is the normal case for a board project, and it is not an error: `ok` is
        True with `has_remote=False`.  Nothing here fetches — ahead/behind is measured
        against the last known remote ref, and saying so is more honest than a hidden
        network call inside a status query.
        """
        with self._lock:
            if not self.is_repository:
                return {"ok": False, "error": "not a project repository"}
            remotes = [r for r in _out(self._git(["remote"])).splitlines() if r.strip()]
            if not remotes:
                return {"ok": True, "has_remote": False, "remotes": [],
                        "detail": "project is local-only"}
            branch = _out(self._git(["branch", "--show-current"]))
            upstream = _out(self._git(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                                       "@{upstream}"]))
            res: Dict[str, Any] = {"ok": True, "has_remote": True, "remotes": remotes,
                                   "branch": branch, "upstream": upstream or None,
                                   "urls": {}}
            for remote in remotes:
                res["urls"][remote] = _out(self._git(["remote", "get-url", remote]))
            if upstream:
                counts = _out(self._git(["rev-list", "--left-right", "--count",
                                         "{}...HEAD".format(upstream)]))
                parts = counts.split()
                if len(parts) == 2 and all(p.isdigit() for p in parts):
                    res["behind"] = int(parts[0])
                    res["ahead"] = int(parts[1])
            res["fetched"] = False
            return res

    def push(self, remote: str = "origin", branch: Optional[str] = None,
             timeout: float = 120.0) -> Dict[str, Any]:
        """Push to a configured remote, proxy env stripped.  Refuses if the remote is absent.

        Guarded on purpose: a board project is local by default, and inventing a remote (or
        letting git prompt for credentials) is not something a chat tool should do silently.
        """
        with self._lock:
            if not self.is_repository:
                return {"ok": False, "error": "not a project repository"}
            remotes = _out(self._git(["remote"])).split()
            if remote not in remotes:
                return {"ok": False, "error": f"no such remote: {remote}",
                        "remotes": remotes,
                        "hint": "git remote add origin <url> in the project directory first"}
            target = branch or _out(self._git(["branch", "--show-current"]))
            if not target:
                return {"ok": False, "error": "detached HEAD — name a branch to push"}
            proc = self._git(["push", "--follow-tags", remote, target],
                             timeout=timeout, strip_proxy=True)
            ok = proc.returncode == 0
            return {"ok": ok, "remote": remote, "branch": target,
                    "exit_code": proc.returncode,
                    "stdout": (proc.stdout or "")[-4000:],
                    "stderr": (proc.stderr or "")[-4000:],
                    "error": None if ok else (proc.stderr or "push failed").strip()[:400]}

    # -- journal convenience -----------------------------------------------------------

    def record(self, actor: str, action: str, args: Any = None, files: Any = None,
               commit: Optional[str] = None, result: Any = None) -> Dict[str, Any]:
        """Append one entry to `.phicad/journal.jsonl` (never raises)."""
        return self.journal.append(actor, action, args=args, files=files, commit=commit,
                                   result=result)


def prepare_generated_project(project_dir: Any, design_path: Any,
                              message: str = "phicad: initial generated design") -> dict:
    """Shared one-shot used by New Project, `/synth build` and the MCP tool."""
    history = ProjectHistory(project_dir)
    # Refuse before copying the design so an unsafe path is entirely side-effect free.
    history._guard()
    imported = history.import_design(design_path)
    status = history.ensure(message=message)
    history.record("synth", "prepare_generated_project",
                   args={"design": str(design_path)},
                   files=[str(imported)] if imported else [],
                   result={"git": status.display()})
    return {
        "ok": status.enabled,
        "project_dir": str(history.project_dir),
        "design": str(imported) if imported else None,
        "git": status.display(),
    }


# --------------------------------------------------------------------------------------
# The coordinator's hook — called after every mutating tool.  Must never raise, ever.
# --------------------------------------------------------------------------------------


def record_and_snapshot(project_path_or_file: Any, actor: str, action: str,
                        args: Any = None, outputs: Optional[Sequence[Any]] = None,
                        mutated: Optional[Sequence[Any]] = None,
                        result_summary: Any = None,
                        autosnapshot: bool = True) -> Dict[str, Any]:
    """Journal one tool action against a project and (optionally) commit what it changed.

    This is the single hook `server.py` calls after any tool that writes: pass the tool's
    own `outputs` / `mutated` lists straight through.  Only files that live inside the
    project repository are committed — a tool writing to the MCP out dir is journaled but
    stages nothing.

    Never raises and never blocks meaningfully: if the path is not inside an initialised
    project repository it returns `{"journaled": False, "reason": ...}` and the caller
    carries on.  It does NOT create repositories — an agent editing a board somewhere on
    disk should not silently acquire a git repo it never asked for; `project_init` does that,
    explicitly.
    """
    try:
        root, reason = locate_project(project_path_or_file)
        if root is None:
            return {"journaled": False, "committed": False, "reason": reason}
        if repository_root(root) is None:
            return {"journaled": False, "committed": False,
                    "reason": f"{root} has no project history yet (call project_init)",
                    "project_dir": str(root)}
        history = ProjectHistory(root, enforce_safety=False)
        inside = history._relativise(list(outputs or []) + list(mutated or []))
        snap: Dict[str, Any] = _no_commit("autosnapshot disabled")
        if autosnapshot and inside:
            try:
                snap = history.snapshot(action, files=inside, actor=actor,
                                        meta={"tool": action})
            except HistoryError as exc:
                snap = _no_commit("commit failed: {}".format(exc))
        elif autosnapshot:
            snap = _no_commit("no changed file inside the project repository")
        entry = history.journal.append(
            actor, action,
            args=args,
            files=inside or [str(p) for p in (list(outputs or []) + list(mutated or []))],
            commit=snap.get("commit"),
            result=result_summary,
            extra={"outputs": [str(p) for p in (outputs or [])],
                   "mutated": [str(p) for p in (mutated or [])]})
        return {"journaled": bool(entry.get("ok")),
                "project_dir": str(root),
                "journal": str(history.journal_path),
                "committed": bool(snap.get("committed")),
                "commit": snap.get("commit"),
                "short": snap.get("short"),
                "changed_files": snap.get("changed_files") or [],
                "insertions": snap.get("insertions", 0),
                "deletions": snap.get("deletions", 0),
                "reason": snap.get("reason"),
                "journal_error": None if entry.get("ok") else entry.get("error")}
    except Exception as exc:                                    # noqa: BLE001 - see docstring
        return {"journaled": False, "committed": False,
                "reason": "record_and_snapshot failed: {}: {}".format(
                    type(exc).__name__, exc)}


# --------------------------------------------------------------------------------------
# CLI — `python3 -m project_history <cmd>` or `python3 project_history.py <cmd>`
# --------------------------------------------------------------------------------------

_CLI_EPILOG = """\
Every command takes a path to a KiCad project directory or to a file inside one
(default: the current directory) and prints human text, or JSON with --json.
"""


def _cli_history(path: Any, require_repo: bool = True):
    root, reason = locate_project(path)
    if root is None:
        return None, {"ok": False, "error": reason, "path": str(path)}
    if require_repo and repository_root(root) is None:
        return None, {"ok": False, "error": f"{root} has no project history",
                      "hint": "phicad project init", "project_dir": str(root)}
    return ProjectHistory(root), None


def _emit(payload: Dict[str, Any], as_json: bool, lines: Optional[List[str]] = None) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        for line in (lines or []):
            print(line)
        if not payload.get("ok", True) and payload.get("error"):
            print("error: {}".format(payload["error"]))
            if payload.get("hint"):
                print("hint:  {}".format(payload["hint"]))
    return 0 if payload.get("ok", True) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for `phicad project ...` (the bash helper shells to this)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="project_history",
        description="Per-project git history and journal for KiCad projects.",
        epilog=_CLI_EPILOG)
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    # `--json` and the project path belong to every subcommand, so they live in a parent
    # parser.  `default=SUPPRESS` matters: without it the subparser writes its own False
    # over a `--json` given before the subcommand, and `project --json status` (the order a
    # human types) would silently print prose.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS, help="machine-readable output")
    common.add_argument("path", nargs="?", default=".",
                       help="project dir or a file in it (default: cwd)")

    def add(name: str, help_text: str):
        return sub.add_parser(name, help=help_text, parents=[common])

    p_init = add("init", "create the project repository and commit a baseline")
    p_init.add_argument("-m", "--message", default="phicad: start project history")
    p_init.add_argument("--design", help="design IR to copy in beside the project")

    p_snap = add("snapshot", "commit the project (all changes, or only --file)")
    p_snap.add_argument("-m", "--message", default="snapshot project changes")
    p_snap.add_argument("--actor", default="user")
    p_snap.add_argument("--file", action="append", dest="files", default=None,
                        help="limit the commit to this file (repeatable)")

    add("status", "branch, HEAD, pending files, commit count")

    p_log = add("log", "recent commits with their actor and files")
    p_log.add_argument("-n", "--limit", type=int, default=20)
    p_log.add_argument("--file", help="only commits touching this file")

    p_diff = add("diff", "git stat between two revisions + the fork's semantic pcb/sch diff")
    p_diff.add_argument("--rev-a", default="HEAD~1")
    p_diff.add_argument("--rev-b", default="HEAD", help="or WORKTREE for uncommitted work")
    p_diff.add_argument("--file")
    p_diff.add_argument("--no-extract", action="store_true",
                        help="skip extracting the documents (implies --no-semantic)")
    p_diff.add_argument("--no-semantic", action="store_true",
                        help="git stat only; do not run the fork's pcb/sch diff")
    p_diff.add_argument("--kicad-cli",
                        help="the fork's kicad-cli (default: PHICAD_KICAD_CLI, PHICAD_BUILD, "
                             "then the build tree beside this checkout)")

    p_res = add("restore", "bring files back from a revision (dry-run unless --apply)")
    p_res.add_argument("rev")
    p_res.add_argument("--file", action="append", dest="files", default=None)
    p_res.add_argument("--apply", action="store_true", help="actually restore and commit")

    p_undo = add("undo", "revert the last commit (dry-run unless --apply)")
    p_undo.add_argument("--apply", action="store_true")

    p_tag = add("tag", "mark HEAD as a milestone")
    p_tag.add_argument("name")
    p_tag.add_argument("-m", "--message", default="")

    p_jou = add("journal", "the tail of .phicad/journal.jsonl")
    p_jou.add_argument("-n", "--limit", type=int, default=50)

    p_watch = add("watch", "one autosnapshot poll; --loop keeps polling")
    p_watch.add_argument("--stable-seconds", type=float, default=2.0)
    p_watch.add_argument("--loop", action="store_true")
    p_watch.add_argument("--interval", type=float, default=1.0)

    add("remote", "remotes and ahead/behind, if any")

    p_push = add("push", "push to a configured remote (refuses if none)")
    p_push.add_argument("--remote", default="origin")
    p_push.add_argument("--branch", default=None)

    # `path` is the optional first positional everywhere; `restore` and `tag` add a required
    # second one.  argparse fills the required one first when only a single argument is
    # given, so both `restore HEAD~1` and `restore /path/to/proj HEAD~1` do the right thing.
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.cmd:
        parser.print_help()
        return 2
    as_json = bool(args.as_json)

    if args.cmd == "init":
        root, reason = locate_project(args.path)
        if root is None:
            return _emit({"ok": False, "error": reason, "path": str(args.path)}, as_json)
        history = ProjectHistory(root)
        already = history.repository_dir is not None
        try:
            status = history.ensure(design_path=args.design, message=args.message)
        except HistoryError as exc:
            return _emit({"ok": False, "error": str(exc),
                          "project_dir": str(root)}, as_json)
        payload = history.status_dict()
        payload["created"] = not already
        payload["already_initialized"] = already
        payload["reason"] = reason
        payload["repository_dir"] = (str(history.repository_dir)
                                      if history.repository_dir else None)
        payload["uses_enclosing_repository"] = history.uses_enclosing_repository
        return _emit(payload, as_json, [str(root), status.display()])

    history, err = _cli_history(args.path)
    if err:
        return _emit(err, as_json)

    if args.cmd == "snapshot":
        try:
            snap = history.snapshot(args.message, files=args.files, actor=args.actor,
                                    meta={"tool": "cli"})
        except HistoryError as exc:
            return _emit({"ok": False, "error": str(exc)}, as_json)
        history.record(args.actor, "cli snapshot", args={"message": args.message},
                       files=snap.get("changed_files"), commit=snap.get("commit"),
                       result={"committed": bool(snap)})
        lines = ["{} {}".format(snap.get("short"), snap.get("message"))] if snap else \
            ["nothing to commit: {}".format(snap.get("reason"))]
        return _emit(dict(snap), as_json, lines)

    if args.cmd == "status":
        payload = history.status_dict()
        lines = [payload.get("display", "")] + \
                ["  {} {}".format(p["state"], p["path"]) for p in payload.get("pending", [])]
        return _emit(payload, as_json, lines)

    if args.cmd == "log":
        rows = history.history(limit=args.limit, path=args.file)
        payload = {"ok": True, "commits": rows, "count": len(rows)}
        lines = ["{}  {}  {}  [{}]".format(r["short"], r["iso"], r["subject"],
                                           r.get("actor") or "-") for r in rows]
        return _emit(payload, as_json, lines)

    if args.cmd == "diff":
        extract = not args.no_extract
        payload = history.diff(args.rev_a, args.rev_b, path=args.file,
                               extract_docs=extract)
        # The semantic half is the reason this beats `git log`, so it runs by DEFAULT here
        # exactly as it does in the project_diff MCP tool -- one surface, one answer.  It is
        # a subprocess to the fork's kicad-cli, because this module must stay stdlib-only.
        if extract and not args.no_semantic:
            payload = attach_semantic(payload, kicad_cli=args.kicad_cli)
        lines = [payload.get("stat", "")]
        for doc in payload.get("semantic", []):
            if doc.get("error"):
                lines.append("{}: semantic diff failed: {}".format(doc.get("path"),
                                                                   doc["error"]))
                continue
            lines.append("{} {} diff: exit {} — {} ({} change{})".format(
                doc.get("path"), doc.get("kind"), doc.get("exit_code"),
                "differs" if doc.get("differs") else "identical",
                doc.get("change_count", 0), "" if doc.get("change_count") == 1 else "s"))
            for kind, count in sorted((doc.get("changes_by_type") or {}).items()):
                lines.append("    {} {}".format(count, kind))
            for change in doc.get("changes", [])[:MAX_CHANGES_ECHOED]:
                # Three properties is a review line; an "added" item carries a dozen and turns
                # the human view into a wall.  The full list is in the JSON and the report.
                shown = (change.get("properties") or [])[:3]
                props = ", ".join(
                    "{} {} → {}".format(p.get("name"), p.get("before"), p.get("after"))
                    for p in shown)
                extra = len(change.get("properties") or []) - len(shown)
                if extra > 0:
                    props += ", +{} more".format(extra)
                lines.append("    {} {} {}{}".format(
                    change.get("kind"), change.get("type"),
                    change.get("refdes") or change.get("uuid") or "",
                    ": " + props if props else ""))
            if doc.get("report_path"):
                lines.append("    report: {}".format(doc["report_path"]))
        if payload.get("semantic_skipped"):
            lines.append("semantic diff skipped: {}".format(payload["semantic_skipped"]))
        for doc in payload.get("documents", []):
            lines.append("kicad doc {}: {} vs {}".format(doc.get("path"), doc.get("a"),
                                                         doc.get("b")))
        return _emit(payload, as_json, lines)

    if args.cmd == "restore":
        payload = history.restore(args.rev, paths=args.files, dry_run=not args.apply)
        lines = ["would change: " if not args.apply else "restored: "] + \
                ["  " + p for p in payload.get("would_change", [])]
        return _emit(payload, as_json, lines)

    if args.cmd == "undo":
        payload = history.undo_last(dry_run=not args.apply)
        lines = ["HEAD {} {}".format(payload.get("head_short"), payload.get("head_subject"))] \
            + ["  " + p for p in payload.get("would_restore", [])]
        return _emit(payload, as_json, lines)

    if args.cmd == "tag":
        payload = history.tag(args.name, args.message)
        return _emit(payload, as_json,
                     ["{} -> {}".format(payload.get("tag"), payload.get("short"))])

    if args.cmd == "journal":
        rows = history.journal.tail(args.limit)
        payload = {"ok": True, "entries": rows, "count": len(rows),
                   "journal": str(history.journal_path)}
        lines = ["{}  {:<14} {:<28} {}".format(
            r.get("ts", ""), str(r.get("actor"))[:14], str(r.get("action"))[:28],
            (r.get("commit") or "-")[:10]) for r in rows]
        return _emit(payload, as_json, lines)

    if args.cmd == "watch":
        if not args.loop:
            snap = history.watch_once(stable_seconds=args.stable_seconds)
            return _emit(dict(snap), as_json,
                         ["committed {}".format(snap.get("short")) if snap
                          else "no commit: {}".format(snap.get("reason"))])
        try:
            while True:
                snap = history.watch_once(stable_seconds=args.stable_seconds)
                if snap:
                    line = json.dumps(dict(snap), default=str) if as_json else \
                        "committed {} {}".format(snap.get("short"), snap.get("message"))
                    print(line, flush=True)
                time.sleep(max(0.1, args.interval))
        except KeyboardInterrupt:
            return 0

    if args.cmd == "remote":
        payload = history.remote_status()
        return _emit(payload, as_json, [json.dumps(payload, default=str)])

    if args.cmd == "push":
        payload = history.push(remote=args.remote, branch=args.branch)
        return _emit(payload, as_json, [(payload.get("stderr") or "").strip()])

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
