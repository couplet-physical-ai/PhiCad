#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Project-level version control for the phicad MCP server.

A standalone KiCad project gets its own Git repository.  A project inside an existing
repository reuses that enclosing repository, with every PhiCad snapshot scoped to the
project directory so a monorepo's unrelated files are never staged.

WHAT THIS EXPOSES (nine plain functions; the server decorates them with @mcp.tool)
    project_init        reuse or `git init` the project repository + commit a baseline
    project_snapshot    commit the project (or named files) with a structured message
    project_status      branch / HEAD / pending files / commit + journal counts
    project_log         recent commits with the actor and tool that made each one
    project_diff        git stat between two revisions + the fork's SEMANTIC pcb/sch diff
    project_restore     bring files back from a revision (dry-run by default)
    project_undo_last   `git revert` HEAD — undo without rewriting history
    project_tag         mark a milestone (e.g. drc-clean-v1)
    project_journal     the tail of .phicad/journal.jsonl (who/what/why per action)
    project_watch_once  one autosnapshot poll, for a caller running its own loop

All of the git mechanics live in `phicad/pylib/project_history.py` (stdlib-only, py3.9-safe)
so the ai-view plugin, the `phicad` bash CLI and this module all speak to the SAME repository
layout and the same journal.  This file adds only the MCP result shape and the one thing the
pylib module deliberately cannot do: run the fork's `kicad-cli pcb diff` / `sch diff` over
two historical revisions of a board.

THE SAFETY RULE THAT MATTERS
    Nothing here initialises a repository the pylib finder refused: a directory containing
    `phicad/`, or anything inside the PhiCad source checkout (detected by a `.git` ancestor
    whose toplevel holds `phicad/bin/phicad`).  `git init` in the fork's own tree, or a
    commit that swallows half of KiCad because a board was opened from `demos/`, is the
    failure mode this whole layer exists to prevent — so `project_init` on the source tree
    returns `ok: false` with the reason, and `demos/` stays un-initialisable by design.

WHY A SNAPSHOT IS TEXT AND A DIFF IS SEMANTIC
    Boards are s-expressions: a `git diff` of one is technically complete and practically
    unreadable (moving a footprint rewrites unrelated lines).  So `project_diff` extracts
    both revisions of every changed `.kicad_pcb` / `.kicad_sch` to temp files and hands them
    to the fork's own differ, which answers in objects — "footprint R12 moved", "net class
    changed".  `kicad-cli ... diff` exits **5** when documents differ; that is a NORMAL
    result, not a failure (this mirrors server.py's `_diff`).

WHAT "ok" MEANS
    `ok` means the git operation ran and did what it promised.  It does NOT mean something
    was committed: a snapshot of an unchanged project is `ok: true, committed: false` with a
    `reason`.  Read `committed`, not `ok`, to know whether history moved.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------------------
# Injected context (configure() fills these in; every tool refuses politely until then)
# --------------------------------------------------------------------------------------

KICAD_CLI: Path | None = None
REPO_ROOT: Path | None = None
OUT_ROOT: Path | None = None

_run: Callable[..., dict[str, Any]] | None = None
_cli_env: Callable[[], dict[str, str]] | None = None
_out_dir: Callable[[str], Path] | None = None
_read_json: Callable[[Path], Any] | None = None

#: The pylib module, imported by path in configure() (see _load_pylib).
ph: Any = None

_REQUIRED = ("KICAD_CLI", "REPO_ROOT", "run", "cli_env", "out_dir", "read_json")

#: A `kicad-cli pcb diff` over two big boards is seconds, not minutes.
DIFF_TIMEOUT_S = 300
#: How many semantic diffs one project_diff call will run (a rename touches many files).
MAX_SEMANTIC_DOCS = 8
#: Cap on the change list echoed back per document; the full report stays on disk.
MAX_CHANGES_ECHOED = 20

_TAG = "project"


def _pick(ctx: Any, name: str) -> Any:
    """Read one dependency out of a dict-ish or attribute-ish context."""
    if isinstance(ctx, dict):
        return ctx.get(name)
    return getattr(ctx, name, None)


def _load_pylib(repo_root: Path) -> Any:
    """Import `phicad/pylib/project_history.py` by path, without polluting sys.path.

    A plain `import project_history` would depend on cwd and could collide with the copy
    the ai-view plugin drops into KiCad's user plugin directory.  Loading the file from the
    worktree the server was pointed at (PHICAD_REPO) keeps the three surfaces in step.
    """
    if ph is not None:
        return ph
    path = repo_root / "phicad" / "pylib" / "project_history.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("phicad_project_history", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("phicad_project_history", module)
    spec.loader.exec_module(module)
    return module


def configure(ctx: Any) -> dict[str, Any]:
    """Bind the server's helpers into this module. Called once, before any tool runs.

    Also loads the stdlib-only git layer from `phicad/pylib/project_history.py`; a missing
    pylib is reported here as `missing: ["project_history"]` rather than surfacing later as
    an import error inside a tool call.
    """
    global KICAD_CLI, REPO_ROOT, OUT_ROOT, _run, _cli_env, _out_dir, _read_json, ph

    missing = [n for n in _REQUIRED if _pick(ctx, n) is None]
    if missing:
        return {"ok": False, "error": "incomplete context", "missing": missing}

    KICAD_CLI = Path(_pick(ctx, "KICAD_CLI"))
    REPO_ROOT = Path(_pick(ctx, "REPO_ROOT"))
    out_root = _pick(ctx, "OUT_ROOT")
    OUT_ROOT = Path(out_root) if out_root else None
    _run = _pick(ctx, "run")
    _cli_env = _pick(ctx, "cli_env")
    _out_dir = _pick(ctx, "out_dir")
    _read_json = _pick(ctx, "read_json")

    ph = _load_pylib(REPO_ROOT)
    if ph is None:
        return {"ok": False, "error": "phicad/pylib/project_history.py not found",
                "missing": ["project_history"],
                "hint": f"expected under {REPO_ROOT}/phicad/pylib/"}

    return {
        "ok": True,
        "kicad_cli": str(KICAD_CLI),
        "kicad_cli_exists": KICAD_CLI.exists(),
        "project_history": str(Path(ph.__file__).resolve()),
        "semantic_diff": KICAD_CLI.exists(),
        "tools": [f.__name__ for f in TOOLS],
    }


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _unconfigured() -> dict[str, Any] | None:
    if _run is None or ph is None:
        return {"ok": False,
                "error": "phicad.mcp.tools_git is not configured",
                "hint": "the server must call tools_git.configure(ctx) at import time"}
    return None


def _project(path: str, require_repo: bool = True) -> tuple[Any, dict[str, Any] | None]:
    """(ProjectHistory, None) or (None, error dict) — resolves and safety-checks a path.

    Accepts a project directory or any file inside one (a board, a schematic, the .kicad_pro)
    so an agent can pass whatever it happens to be holding.
    """
    root, reason = ph.locate_project(path)
    if root is None:
        return None, {"ok": False, "error": reason, "path": str(path),
                      "hint": "copy the project out of the source tree and pass that path"}
    if require_repo and ph.repository_root(root) is None:
        return None, {"ok": False, "error": f"{root} has no project history yet",
                      "project_dir": str(root), "hint": "call project_init first"}
    return ph.ProjectHistory(root), None


def _snap_dict(snap: Any) -> dict[str, Any]:
    """A SnapshotResult as a plain JSON-safe dict (it is dict-like but bool-overridden)."""
    return {k: v for k, v in dict(snap).items()}


def _semantic_diff(doc: dict[str, Any]) -> dict[str, Any]:
    """Run the fork's own differ on two extracted revisions of one board/schematic.

    `kicad-cli {pcb,sch} diff --format json -o <report> <a> <b>`: exit 0 = identical,
    exit 5 = documents differ (a normal answer), anything else is a real failure.  Returns
    the change counts and the first few changes; the whole report stays on disk at
    `report_path` because a board diff can be thousands of entries.
    """
    suffix = Path(doc["path"]).suffix
    kind = "pcb" if suffix == ".kicad_pcb" else "sch"
    a, b = Path(doc["a"]), Path(doc["b"])
    stem = f"{a.stem}--{b.stem}"[:80]
    report = _out_dir(_TAG) / f"{stem}.{kind}diff.json"
    argv = [str(KICAD_CLI), kind, "diff", "--format", "json", "-o", str(report),
            str(a), str(b)]
    # The semantic diff can land on stdout rather than the report file: never clip it.
    res = _run(argv, timeout=DIFF_TIMEOUT_S, env=_cli_env(), ok_codes=(0, 5),
               stdout_limit=None)
    out: dict[str, Any] = {
        "path": doc["path"],
        "kind": kind,
        "a": str(a),
        "b": str(b),
        "ok": bool(res.get("ok")),
        "exit_code": res.get("exit_code"),
        "differs": res.get("exit_code") == 5,
        "identical": res.get("exit_code") == 0,
    }
    if not res.get("ok"):
        out["error"] = (res.get("error")
                        or (res.get("stderr") or "").strip()[:400]
                        or f"kicad-cli {kind} diff exit {res.get('exit_code')}")
        return out

    payload = _read_json(report) if report.exists() else None
    if payload is None and (res.get("stdout") or "").lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(res["stdout"])
        except Exception:
            payload = None
    if report.exists():
        out["report_path"] = str(report)
    if isinstance(payload, dict):
        changes = payload.get("changes") or []
        kinds: dict[str, int] = {}
        types: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for ch in changes:
            if not isinstance(ch, dict):
                continue
            k = str(ch.get("kind", "?"))
            t = str(ch.get("typeName", "?"))
            kinds[k] = kinds.get(k, 0) + 1
            types[t] = types.get(t, 0) + 1
            if len(items) < MAX_CHANGES_ECHOED:
                items.append({
                    "kind": k,
                    "type": t,
                    "refdes": ch.get("refdes"),
                    "uuid": ch.get("id"),
                    "properties": [
                        {"name": p.get("name"),
                         "before": (p.get("before") or {}).get("v"),
                         "after": (p.get("after") or {}).get("v")}
                        for p in (ch.get("properties") or []) if isinstance(p, dict)
                    ][:8],
                })
        out["doc_type"] = payload.get("docType")
        out["change_count"] = len(changes)
        out["changes_by_kind"] = kinds
        out["changes_by_type"] = types
        out["changes"] = items
    return out


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


def project_init(path: str, message: str | None = None) -> dict[str, Any]:
    """Initialize history for a KiCad project without nesting inside an existing repo.

    A standalone project gets a repository at its root; a project below an existing
    repository reuses that repository and scopes its baseline commit to this project.
    `path` is the project directory or any file inside it. Writes `.phicad/project.json`,
    appends PhiCad's block to `.gitignore` (KiCad lock/backup files, the host-bridge
    descriptor and the journal stay untracked) and commits everything else as
    `phicad(init): <message>`. Safe to call twice: an existing repository is left alone and
    only genuinely new files are committed.

    Also installs the fork's STRUCTURAL merge driver — `.gitattributes` routing
    `*.kicad_pcb`/`_sch`/`_sym`/`_mod` to `kicad-cli git-mergedriver`, plus the per-repo
    `merge.kicad-*` config — because git's default text merge writes `<<<<<<<` into a board
    and the result will not load at all. Both halves are append-only: a glob or config key
    someone already chose is never overwritten. Reported as `merge_driver`, whose
    `installed: false` (with a `reason`) means merges fall back to text merge, so check it
    before handing one project repo to a second agent.

    REFUSED, deliberately: a directory containing `phicad/`, anything inside the PhiCad
    source checkout, $HOME, and an existing repository path that does not identify one KiCad
    project — `ok: false` with the reason. Never creates a nested repository silently.
    """
    if (err := _unconfigured()):
        return err
    root, reason = ph.locate_project(path)
    if root is None:
        return {"ok": False, "error": reason, "path": str(path),
                "hint": "copy the project out of the source tree and pass that path"}
    already = ph.repository_root(root) is not None
    history = ph.ProjectHistory(root)
    try:
        status = history.ensure(message=message or "phicad: start project history")
    except Exception as exc:                                   # noqa: BLE001 - never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "project_dir": str(root)}
    out = history.status_dict()
    out["ok"] = bool(out.get("ok")) and status.enabled
    out["created"] = not already
    out["already_initialized"] = already
    out["reason"] = reason
    out["repository_dir"] = (str(history.repository_dir) if history.repository_dir else None)
    out["uses_enclosing_repository"] = history.uses_enclosing_repository
    out["gitignore"] = str(root / ".gitignore")
    out["journal"] = str(history.journal_path)
    out["outputs"] = [str(root / ".phicad" / ph.CONFIG_NAME), str(root / ".gitignore")]
    # Whether this repo is safe for a `git merge` that touches boards -- an agent about to
    # hand the project to a second agent needs to know, and `installed: false` says why.
    out["merge_driver"] = dict(getattr(history, "last_merge_driver", {}) or {})
    if (root / ".gitattributes").exists():
        out["outputs"].append(str(root / ".gitattributes"))
    return out


def project_snapshot(path: str, message: str, actor: str = "agent",
                     files: list[str] | None = None) -> dict[str, Any]:
    """Commit the project's current state as one auditable revision.

    The subject is `phicad(<actor>): <message>` and the body carries `Phicad-Actor:`,
    `Phicad-Tool:` and `Phicad-Files:` trailers, so `git log` alone says who changed a board
    and why. `files` limits the commit to those paths (anything outside the project repo is
    dropped) — without it every change in the project is staged.

    `ok: true, committed: false` with a `reason` when there was nothing to commit; that is a
    normal answer, not an error. Read `committed`, `changed_files`, `insertions`,
    `deletions`. Journals the action to `.phicad/journal.jsonl` either way.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    try:
        snap = history.snapshot(message, files=files, actor=actor or "agent",
                                meta={"tool": "project_snapshot"})
    except Exception as exc:                                   # noqa: BLE001 - never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "project_dir": str(history.project_dir)}
    out = _snap_dict(snap)
    history.record(actor or "agent", "project_snapshot",
                   args={"message": message, "files": files},
                   files=out.get("changed_files"), commit=out.get("commit"),
                   result={"committed": out.get("committed")})
    out["project_dir"] = str(history.project_dir)
    out["actor"] = actor or "agent"
    out["mutated"] = []
    out["outputs"] = []
    return out


def project_status(path: str) -> dict[str, Any]:
    """Report the project repository's branch, HEAD, pending changes and commit count.

    Read-only. `clean` is False when the working tree has uncommitted changes; `pending`
    lists them with their porcelain state (`M`, `??`, …). Also returns the committer identity
    the project will use and how many journal entries have been recorded. An uninitialised
    project is `ok: false` with the hint to call project_init.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    out = history.status_dict()
    out["journal"] = str(history.journal_path)
    out["tags"] = history.tags()
    remote = history.remote_status()
    out["remote"] = {k: v for k, v in remote.items() if k != "ok"}
    return out


def project_log(path: str, limit: int = 20, file: str | None = None) -> dict[str, Any]:
    """List recent commits of the project with the actor and tool behind each one.

    Read-only. Each entry is `{sha, short, ts, iso, subject, author, actor, tool, files}`,
    newest first; `actor`/`tool` come from the `Phicad-*` trailers a PhiCad snapshot writes
    (they are None for commits made by hand or by `git`). `file` restricts the log to the
    history of one file (renames followed).
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    rows = history.history(limit=max(1, int(limit)), path=file)
    return {"ok": True, "project_dir": str(history.project_dir),
            "count": len(rows), "limit": int(limit), "file": file, "commits": rows}


def project_diff(path: str, rev_a: str = "HEAD~1", rev_b: str = "HEAD",
                 semantic: bool = True) -> dict[str, Any]:
    """Diff two revisions of a project: git stat plus the fork's SEMANTIC board/schematic diff.

    Read-only with respect to the project. `git diff --numstat/--stat` gives the file-level
    picture; then, with `semantic=True`, every changed `.kicad_pcb` / `.kicad_sch` is
    extracted from BOTH revisions to temp files and compared with `kicad-cli {pcb,sch} diff
    --format json` — which answers in objects ("footprint R12 moved", "track added") instead
    of s-expression line noise. Pass `rev_b="WORKTREE"` to diff committed history against
    uncommitted edits.

    Per document you get `differs`, `change_count`, `changes_by_kind`, `changes_by_type`,
    the first 20 changes and `report_path` for the full JSON. `differs` comes from
    kicad-cli's exit code 5, which means "documents differ" and is a SUCCESS. `semantic` is
    silently skipped (with `semantic_skipped`) when kicad-cli is not built.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    dest = _out_dir(_TAG) / "revs"
    dest.mkdir(parents=True, exist_ok=True)
    out = history.diff(rev_a, rev_b, extract_docs=True, dest_dir=dest)
    if not out.get("ok"):
        out.setdefault("project_dir", str(history.project_dir))
        return out
    out["semantic_requested"] = bool(semantic)
    docs = out.get("documents") or []
    if not semantic:
        out["semantic_skipped"] = "not requested"
        return out
    # "nothing to diff" is checked BEFORE the binary: an agent asking about a revision range
    # that touched no board wants that answer, not a build complaint it cannot act on.
    if not docs:
        out["semantic_skipped"] = "no .kicad_pcb/.kicad_sch changed between these revisions"
        return out
    if not KICAD_CLI.exists():
        out["semantic_skipped"] = f"kicad-cli not built at {KICAD_CLI}"
        out["hint"] = "phicad/bin/phicad build kicad-cli"
        return out
    results = []
    for doc in docs[:MAX_SEMANTIC_DOCS]:
        if doc.get("error"):
            results.append({"path": doc.get("path"), "ok": False, "error": doc["error"]})
            continue
        results.append(_semantic_diff(doc))
    out["semantic"] = results
    if len(docs) > MAX_SEMANTIC_DOCS:
        out["semantic_truncated"] = len(docs) - MAX_SEMANTIC_DOCS
    out["semantic_change_count"] = sum(r.get("change_count") or 0 for r in results)
    out["ok"] = all(r.get("ok") for r in results)
    if not out["ok"]:
        out["error"] = "one or more semantic diffs failed — see semantic[].error"
    return out


def project_restore(path: str, rev: str, files: list[str] | None = None,
                    dry_run: bool = True) -> dict[str, Any]:
    """Bring files back from an earlier revision — the "undo a bad edit" tool.

    `dry_run=True` (the DEFAULT) writes nothing and only reports `would_change`. With
    `dry_run=False` the files are checked out from `rev` AND committed as
    `phicad(restore): …` with a `Phicad-Source-Rev:` trailer, so the recovery is itself part
    of the audit trail (a bare checkout would leave the project dirty and let the autosnapshot
    watcher describe a rollback as an edit). History is never rewritten. `files` limits the
    restore; without it the whole project is restored to that revision.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    try:
        out = history.restore(rev, paths=files, dry_run=bool(dry_run))
    except Exception as exc:                                   # noqa: BLE001 - never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "project_dir": str(history.project_dir)}
    out["project_dir"] = str(history.project_dir)
    if isinstance(out.get("snapshot"), dict):
        out["snapshot"] = _snap_dict(out["snapshot"])
    root = history.project_dir
    changed = [str(root / p) for p in out.get("would_change", [])]
    out["mutated"] = [] if dry_run else changed
    out["outputs"] = []
    history.record("restore", "project_restore",
                   args={"rev": rev, "files": files, "dry_run": bool(dry_run)},
                   files=changed,
                   commit=(out.get("snapshot") or {}).get("commit"),
                   result={"restored": out.get("restored")})
    return out


def project_undo_last(path: str, dry_run: bool = True) -> dict[str, Any]:
    """Undo the most recent commit with `git revert` — history is preserved, not rewritten.

    A revert adds a commit that inverts HEAD, so the undone state stays reachable and the
    undo can itself be undone. `dry_run=True` (the DEFAULT) reports the HEAD subject and the
    files that would be put back. A dirty working tree is REFUSED rather than folded into the
    revert, because that would attribute the designer's unsaved edits to the undo — snapshot
    first. Refused on a repository whose HEAD is the initial commit.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    try:
        out = history.undo_last(dry_run=bool(dry_run))
    except Exception as exc:                                   # noqa: BLE001 - never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "project_dir": str(history.project_dir)}
    out["project_dir"] = str(history.project_dir)
    root = history.project_dir
    changed = [str(root / p) for p in out.get("would_restore", [])]
    out["mutated"] = [] if dry_run else changed
    out["outputs"] = []
    history.record("undo", "project_undo_last",
                   args={"dry_run": bool(dry_run)}, files=changed,
                   commit=(out.get("revert_commit") or {}).get("commit"),
                   result={"reverted": out.get("reverted"), "error": out.get("error")})
    return out


def project_tag(path: str, name: str, message: str = "") -> dict[str, Any]:
    """Mark the project's current revision as a milestone (an annotated git tag).

    For the states worth naming: `drc-clean-v1`, `sent-to-fab-2026-03`, `rev-b`. The tag
    points at HEAD, so snapshot first if there are pending changes — a milestone that does
    not include the last save is worse than no milestone. An existing tag name is refused
    rather than moved (`ok: false`), because moving a tag rewrites what a colleague already
    fetched.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    try:
        out = history.tag(name, message)
    except Exception as exc:                                   # noqa: BLE001 - never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "project_dir": str(history.project_dir)}
    out["project_dir"] = str(history.project_dir)
    status = history.status_dict()
    out["clean_at_tag"] = status.get("clean")
    if not status.get("clean"):
        out["warning"] = ("the working tree had uncommitted changes; the tag marks HEAD, "
                          "not what is on disk")
    history.record("user", "project_tag", args={"name": name, "message": message},
                   commit=out.get("commit"), result={"ok": out.get("ok")})
    return out


def project_journal(path: str, limit: int = 50) -> dict[str, Any]:
    """Read the project's action journal: who did what to this board, in order.

    Read-only. Every mutating PhiCad tool appends one JSON line to `.phicad/journal.jsonl`
    with `{ts, actor, action, args, files, commit, result}` — the commit sha ties an entry to
    the revision it produced, and argument values whose key looks like a token/secret/key are
    stored as `***`. The journal is gitignored (it is written on every action; tracking it
    would make the autosnapshot watcher commit its own noise), so this is the only way to
    read it back. Returns the LAST `limit` entries.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path, require_repo=False)
    if err:
        return err
    rows = history.journal.tail(max(1, int(limit)))
    return {"ok": True, "project_dir": str(history.project_dir),
            "journal": str(history.journal_path),
            "exists": history.journal_path.exists(),
            "count": len(rows), "total": history.journal.count(),
            "limit": int(limit), "entries": rows}


def project_watch_once(path: str, stable_seconds: float = 2.0) -> dict[str, Any]:
    """One poll of the save watcher: commit the project once its files have stopped changing.

    Designed to be called from a loop (the ai-view plugin does exactly this). A KiCad save
    rewrites several files in quick succession, so this commits only after the porcelain
    status has been IDENTICAL for `stable_seconds` — otherwise it returns
    `committed: false` with a `reason` (`changes still arriving`, `waiting for the save to
    settle`, `working tree is clean`) and the caller polls again. The stability window is
    per-call-site state, so the first call on a freshly dirty tree never commits;
    that is intentional, not a miss.

    Commits as `phicad(editor): autosnapshot <UTC timestamp>`.
    """
    if (err := _unconfigured()):
        return err
    history, err = _project(path)
    if err:
        return err
    try:
        snap = history.watch_once(stable_seconds=float(stable_seconds))
    except Exception as exc:                                   # noqa: BLE001 - never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "project_dir": str(history.project_dir)}
    out = _snap_dict(snap)
    out["project_dir"] = str(history.project_dir)
    out["stable_seconds"] = float(stable_seconds)
    if out.get("committed"):
        history.record("editor", "project_watch_once", args={},
                       files=out.get("changed_files"), commit=out.get("commit"),
                       result={"committed": True})
    return out


# --------------------------------------------------------------------------------------
# The coordinator's hook — re-exported so server.py imports it from one place
# --------------------------------------------------------------------------------------


def record_and_snapshot(project_path_or_file: Any, actor: str, action: str,
                        args: Any = None, outputs: Any = None, mutated: Any = None,
                        result_summary: Any = None,
                        autosnapshot: bool = True) -> dict[str, Any]:
    """Journal one tool action against a project and commit the files it changed.

    Call this after every mutating tool: pass the tool's own `outputs` / `mutated` lists
    straight through. Only files inside the project repository are staged, so a tool writing
    to the MCP out dir is journaled and commits nothing.

    NEVER raises and never creates a repository. If the path is not inside an initialised
    project repo it returns `{"journaled": false, "reason": ...}` and the caller carries on —
    an agent editing a board somewhere on disk must not silently acquire a git repo it never
    asked for; `project_init` does that, explicitly.
    """
    if ph is None:
        return {"journaled": False, "committed": False,
                "reason": "tools_git is not configured"}
    return ph.record_and_snapshot(project_path_or_file, actor, action, args=args,
                                  outputs=outputs, mutated=mutated,
                                  result_summary=result_summary,
                                  autosnapshot=autosnapshot)


#: What the server registers. Kept as data so registration cannot drift from the module.
TOOLS = (project_init, project_snapshot, project_status, project_log, project_diff,
         project_restore, project_undo_last, project_tag, project_journal,
         project_watch_once)
