#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""The Python side of ``phicad core apply``.

The JS service owns the bounded child process and the fixed argument vector.  This module owns
Circuit JSON translation, the existing synth build, and project history.  It stages all native
files outside the target project, then performs one atomic file replacement followed by one
``ProjectHistory.snapshot`` and one journal record.  A rejected plan never calls the synth
writer, and a failed materialization restores the target bytes before returning.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
_PARENT = _HERE.parent
_PYLIB = _PARENT / "pylib"
for _path in (_HERE, _PYLIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from circuit_json import Rejected, Translated, translate  # noqa: E402

# Import project_history by its file path, matching synth/cli.py.  The module contains dataclasses
# whose class body expects the module to have been registered before exec_module.
_HISTORY_PATH = _PYLIB / "project_history.py"
_HISTORY_SPEC = importlib.util.spec_from_file_location("phicad_project_history_apply", _HISTORY_PATH)
if _HISTORY_SPEC is None or _HISTORY_SPEC.loader is None:  # pragma: no cover - broken checkout
    raise ImportError(f"cannot load {_HISTORY_PATH}")
_HISTORY = importlib.util.module_from_spec(_HISTORY_SPEC)
sys.modules.setdefault("phicad_project_history_apply", _HISTORY)
_HISTORY_SPEC.loader.exec_module(_HISTORY)
ProjectHistory = _HISTORY.ProjectHistory
HistoryError = _HISTORY.HistoryError

SCHEMA_PLAN = "io.github.couplet-physical-ai.phicad.apply-plan/v1"
SCHEMA_RESULT = "io.github.couplet-physical-ai.phicad.apply-result/v1"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_OUTPUT_FILES = 256


class ApplyFailure(RuntimeError):
    """A failure that can be represented as rejected or unavailable without a traceback."""

    def __init__(self, status: str, reason: str, code: str = "") -> None:
        self.status = status
        self.reason = reason
        self.code = code or reason
        super().__init__(reason)


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    payload = _json_bytes(document)
    if len(payload) > MAX_RESULT_BYTES:
        raise ApplyFailure("unavailable", f"JSON output exceeds {MAX_RESULT_BYTES} bytes", "output_limit")
    if path.is_symlink():
        raise ApplyFailure("unavailable", f"output path is a symlink: {path}", "output_symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ApplyFailure("rejected", f"{label} is a symlink", "symlink_input")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ApplyFailure("unavailable", f"cannot stat {label}: {exc}", "input_unavailable") from exc
    if not stat.S_ISREG(mode):
        raise ApplyFailure("rejected", f"{label} is not a regular file", "invalid_input")


def _project_paths(raw: str) -> Tuple[Path, Path, Path, str]:
    given = Path(raw).expanduser()
    if given.suffix == ".kicad_pro":
        project_dir = given.parent.resolve()
        pro = given.resolve()
        stem = given.stem
    else:
        project_dir = given.resolve()
        if not project_dir.exists() or not project_dir.is_dir():
            # Planning can still describe a future project; commit will report unavailable.
            stem = project_dir.name or "project"
            pro = project_dir / f"{stem}.kicad_pro"
        else:
            candidates = sorted(p for p in project_dir.glob("*.kicad_pro") if not p.is_symlink())
            if candidates:
                pro = candidates[0].resolve()
                stem = pro.stem
            else:
                stem = project_dir.name or "project"
                pro = project_dir / f"{stem}.kicad_pro"
    board = project_dir / f"{stem}.kicad_pcb"
    if project_dir.is_dir():
        _regular_file(pro, "project file") if pro.exists() else None
        if not board.exists():
            boards = sorted(p for p in project_dir.glob("*.kicad_pcb") if not p.is_symlink())
            if boards:
                board = boards[0]
        if board.exists():
            _regular_file(board, "board file")
    return project_dir, pro, board, stem


def _minimal_env(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "USER", "SHELL")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"NO_COLOR": "1", "CI": "1", "KICAD_RUN_FROM_BUILD_DIR": "1"})
    for key in ("PHICAD_KICAD_CLI", "PHICAD_BUILD", "PHICAD_VENV", "PHICAD_SYNTH_PYTHON"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if extra:
        env.update(extra)
    return env


def _synth_python() -> str:
    """The interpreter for the synth build subprocess.

    The netlist import stage needs kipy, which lives in the automation venv rather than the
    system interpreter, so the venv wins when one is available.  The service itself stays on
    the stdlib-only caller interpreter.
    """
    explicit = os.environ.get("PHICAD_SYNTH_PYTHON", "").strip()
    if explicit and Path(explicit).is_file() and os.access(explicit, os.X_OK):
        return explicit
    venv = os.environ.get("PHICAD_VENV", "").strip()
    if venv:
        candidate = Path(venv).expanduser() / "bin" / "python"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    fallback = _PARENT.parent / "build" / "venv" / "bin" / "python"
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    return sys.executable


def _kicad_cli() -> Optional[Path]:
    explicit = os.environ.get("PHICAD_KICAD_CLI", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
    # The JS caller is expected to pass PHICAD_KICAD_CLI.  This fallback is only the current
    # checkout's build tree, never an installed stable KiCad or another worktree.
    import build_layout  # phicad/pylib, on sys.path above
    candidate = build_layout.kicad_cli_in(build_layout.default_build_dir(_PARENT.parent))
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _kicad_version(cli: Optional[Path]) -> str:
    if cli is None:
        return "unknown"
    try:
        proc = subprocess.run([str(cli), "--version"], capture_output=True, text=True,
                              timeout=2, env=_minimal_env())
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return ((proc.stdout or proc.stderr).strip().splitlines() or ["unknown"])[0]


def _head(history: ProjectHistory) -> Optional[str]:
    if not history.repository_dir:
        return None
    proc = history._git(["rev-parse", "HEAD"], timeout=10.0)
    value = (proc.stdout or "").strip()
    return value if proc.returncode == 0 and value else None


def _commit_count(history: ProjectHistory) -> Optional[int]:
    if not history.repository_dir:
        return None
    proc = history._git(["rev-list", "--count", "HEAD"], timeout=10.0)
    value = (proc.stdout or "").strip()
    return int(value) if proc.returncode == 0 and value.isdigit() else None


def _project_dirty(history: ProjectHistory) -> bool:
    status = history.status()
    return bool(status.enabled and not status.clean)


def _base_info(project_dir: Path, board: Path) -> Tuple[Optional[str], Optional[str], ProjectHistory]:
    history = ProjectHistory(project_dir)
    return _head(history), _sha256_file(board if board.exists() else None), history


def _base_plan(circuit_hash: str, project_dir: Path, base_revision: Optional[str],
               board_hash: Optional[str], counts: Mapping[str, int],
               supported: Sequence[str], rejected_types: Sequence[str], operations: Sequence[Mapping[str, Any]],
               rejections: Sequence[Mapping[str, str]], tools: Mapping[str, str]) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "schema": SCHEMA_PLAN,
        "status": "rejected" if rejections else "planned",
        "circuit_json_hash": circuit_hash,
        "project": {
            "path": str(project_dir), "base_revision": base_revision, "base_board_hash": board_hash,
        },
        "counts": dict(counts),
        "coverage": {"supported": sorted(set(supported)), "rejected": sorted(set(rejected_types))},
        "operations": list(operations),
        "rejections": list(rejections),
        "plan_hash": "",
        "tool_versions": dict(tools),
    }
    plan["plan_hash"] = _sha256_bytes(_json_bytes(plan))
    return plan


def _add_plan_rejection(plan: Dict[str, Any], code: str, element: str, detail: str) -> Dict[str, Any]:
    entries = list(plan.get("rejections", []))
    if not any(item.get("code") == code and item.get("element") == element for item in entries):
        entries.append({"code": code, "element": element, "detail": detail})
    entries.sort(key=lambda item: (item.get("code", ""), item.get("element", ""), item.get("detail", "")))
    plan["rejections"] = entries
    plan["status"] = "rejected"
    rejected = set(plan.get("coverage", {}).get("rejected", []))
    rejected.add(element)
    plan["coverage"]["rejected"] = sorted(rejected)
    plan["coverage"]["supported"] = sorted(set(plan["coverage"].get("supported", [])) - {element})
    plan["plan_hash"] = ""
    plan["plan_hash"] = _sha256_bytes(_json_bytes(plan))
    return plan


def _load_json(source: Path) -> Tuple[bytes, Any]:
    _regular_file(source, "Circuit JSON input")
    payload = source.read_bytes()
    if len(payload) > MAX_SOURCE_BYTES:
        raise ApplyFailure("rejected", f"Circuit JSON exceeds {MAX_SOURCE_BYTES} bytes", "source_too_large")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value}")

    try:
        return payload, json.loads(payload.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ApplyFailure("rejected", f"invalid Circuit JSON: {exc}", "invalid_circuit_json") from exc


def _translated(raw: Any, project_stem: str) -> Tuple[Optional[Translated], List[Dict[str, str]]]:
    try:
        if not isinstance(raw, list):
            raise Rejected("invalid_circuit_json", "Circuit JSON root must be an array")
        return translate(raw, project_stem, board_name=project_stem), []
    except Rejected as exc:
        return None, [{"code": exc.code, "element": exc.element or "circuit_json", "detail": exc.detail}]


def make_plan(source: Path, project: str) -> Tuple[Dict[str, Any], Optional[Translated], Path, Path, ProjectHistory]:
    project_dir, _pro, board, stem = _project_paths(project)
    base_revision, board_hash, history = _base_info(project_dir, board)
    try:
        payload, raw = _load_json(source)
        circuit_hash = _sha256_bytes(payload)
    except ApplyFailure as exc:
        payload = source.read_bytes() if source.exists() and source.is_file() else b""
        circuit_hash = _sha256_bytes(payload)
        plan = _base_plan(circuit_hash, project_dir, base_revision, board_hash,
                          {"components": 0, "pads": 0, "nets": 0, "traces": 0, "vias": 0, "zones": 0},
                          [], [], [], [{"code": exc.code, "element": "circuit_json", "detail": exc.reason}],
                          {"phicad": "native-apply-v1", "kicad_cli": _kicad_version(_kicad_cli()),
                           "tscircuit_circuit_json": "unknown"})
        return plan, None, project_dir, board, history

    translated, initial_rejections = _translated(raw, stem)
    if translated is None:
        counts = {"components": 0, "pads": 0, "nets": 0, "traces": 0, "vias": 0, "zones": 0}
        plan = _base_plan(circuit_hash, project_dir, base_revision, board_hash, counts,
                          [], [], [], initial_rejections,
                          {"phicad": "native-apply-v1", "kicad_cli": _kicad_version(_kicad_cli()),
                           "tscircuit_circuit_json": "unknown"})
    else:
        plan = _base_plan(
            circuit_hash, project_dir, base_revision, board_hash, translated.counts,
            translated.supported, translated.rejected_types, translated.operations,
            translated.rejections,
            {"phicad": "native-apply-v1", "kicad_cli": _kicad_version(_kicad_cli()),
             "tscircuit_circuit_json": "unknown"},
        )
    return plan, translated, project_dir, board, history


def _pipeline(project_dir: Path, translated: Translated, cli: Path) -> Tuple[Path, Dict[str, Any]]:
    """Run the existing full synth pipeline in a temp project outside the target."""
    temporary_root = Path(tempfile.mkdtemp(prefix="phicad-apply-"))
    ir_path = temporary_root / f"{translated.design_ir['project']}.design.json"
    out_dir = temporary_root / "native"
    socket = temporary_root / "synth.sock"
    ir_path.write_bytes(_json_bytes(translated.design_ir))
    command = [
        _synth_python(), str(_HERE / "cli.py"), "build", str(ir_path),
        "--output", str(out_dir), "--force", "--json", "--socket", str(socket),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60,
                              cwd=str(_HERE), env=_minimal_env({"PHICAD_KICAD_CLI": str(cli)}))
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise ApplyFailure("unavailable", "synth pipeline exceeded the 60 second limit", "timeout") from exc
    try:
        report = json.loads(proc.stdout) if proc.stdout.strip() else {
            "ok": False, "error": (proc.stderr or "synth produced no JSON report").strip()[-1000:]
        }
    except json.JSONDecodeError:
        report = {"ok": False, "error": "synth produced invalid JSON", "stderr": proc.stderr[-1000:]}
    if proc.returncode != 0 or not report.get("ok"):
        failed = report.get("failed") or []
        detail = "synth pipeline failed"
        if failed:
            detail += ": " + ", ".join(str(item) for item in failed)
        elif report.get("error"):
            detail += ": " + str(report["error"])
        report["stderr_tail"] = (proc.stderr or "")[-1000:]
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise ApplyFailure("rejected", detail, "pipeline_failed")

    if not out_dir.is_dir():
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise ApplyFailure("rejected", "synth produced no project directory", "pipeline_failed")
    report["_temporary_root"] = str(temporary_root)
    return out_dir, report


def _native_stage_files(stage: Path, stem: str) -> List[Path]:
    # Exact stem match.  `startswith` also caught `<stem>.exported.net` (the ERC cross-check
    # artifact), and the transaction then wrote it over the real netlist.
    wanted_suffixes = {".kicad_pro", ".kicad_pcb", ".kicad_sch", ".net"}
    files = sorted(p for p in stage.iterdir()
                   if p.is_file() and not p.is_symlink() and p.stem == stem
                   and p.suffix in wanted_suffixes)
    if not any(p.suffix == ".kicad_pcb" for p in files) or not any(p.suffix == ".kicad_pro" for p in files):
        raise ApplyFailure("rejected", "synth did not produce a native project and board", "pipeline_failed")
    if len(files) > MAX_OUTPUT_FILES:
        raise ApplyFailure("unavailable", "synth produced too many output files", "output_limit")
    return files


def _copy_transaction(project_dir: Path, stage_files: Sequence[Path], stem: str,
                      history: ProjectHistory) -> Tuple[List[str], Dict[Path, Optional[bytes]]]:
    """Replace native files, retaining exact bytes for rollback."""
    backups: Dict[Path, Optional[bytes]] = {}
    changed: List[str] = []
    for staged in stage_files:
        target = project_dir / staged.name
        if target.exists() or target.is_symlink():
            _regular_file(target, f"target output {target.name}")
            old = target.read_bytes()
            backups[target] = old
            if old == staged.read_bytes():
                continue
        else:
            backups[target] = None
        changed.append(target.relative_to(project_dir).as_posix())

    if not changed:
        return changed, backups

    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        for staged in stage_files:
            target = project_dir / staged.name
            if target.relative_to(project_dir).as_posix() not in changed:
                continue
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(project_dir))
            try:
                with os.fdopen(fd, "wb") as handle:
                    with staged.open("rb") as source:
                        shutil.copyfileobj(source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
    except Exception:
        _restore_transaction(backups)
        raise
    return changed, backups


def _ensure_journal_ignored(project_dir: Path, backups: Dict[Path, Optional[bytes]]) -> Optional[str]:
    """Write project_history's gitignore block if `.phicad/journal.jsonl` is not yet ignored.

    Without this the journal record makes the working tree permanently dirty, and the second
    apply is rejected for a mess the first apply made.  The write joins the same transaction:
    it is backed up for rollback and committed inside the same single revision.
    """
    gitignore = project_dir / ".gitignore"
    if gitignore.is_symlink():
        raise ApplyFailure("rejected", ".gitignore is a symlink", "symlink_input")
    previous = gitignore.read_text(encoding="utf-8") if gitignore.exists() else None
    if previous is not None and ".phicad/journal.jsonl" in previous.splitlines():
        return None
    backups.setdefault(gitignore, previous.encode("utf-8") if previous is not None else None)
    probe = ProjectHistory(project_dir, enforce_safety=False)
    probe._write_gitignore()
    return ".gitignore"


def _restore_transaction(backups: Mapping[Path, Optional[bytes]]) -> None:
    for target, old in backups.items():
        if old is None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            continue
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.rollback.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(old)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _result(status: str, plan: Mapping[str, Any], before: Optional[str], after: Optional[str],
            undo_entries: int, board: Path, reason: Optional[str]) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_RESULT,
        "status": status,
        "plan_hash": plan.get("plan_hash"),
        "revision": {"before": before, "after": after, "undo_entries": undo_entries},
        "board": {"path": str(board), "sha256": _sha256_file(board)},
        "reason": reason,
    }


def apply(source: Path, project: str, plan_path: Path, result_path: Optional[Path] = None,
          expected_revision: Optional[str] = None, commit: bool = False) -> Dict[str, Any]:
    plan, translated, project_dir, board, history = make_plan(source, project)
    before = plan["project"]["base_revision"]

    if expected_revision is not None and expected_revision != before:
        _add_plan_rejection(
            plan, "stale_revision", "project",
            f"expected revision {expected_revision!r}, current revision is {before!r}",
        )

    _write_json(plan_path, plan)
    if not commit:
        return {"exit_code": 0 if plan["status"] == "planned" else 3,
                "plan": plan, "result": None, "pipeline": None}

    if plan["status"] != "planned" or translated is None:
        result = _result("rejected", plan, before, None, 0, board,
                         plan["rejections"][0]["detail"] if plan["rejections"] else "plan rejected")
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 3, "plan": plan, "result": result, "pipeline": None}

    # Re-check all optimistic preconditions after writing the plan.  The plan is outside the
    # project, so a concurrent editor can only make the native target stale, never partly apply it.
    now_before, now_board_hash, _ = _base_info(project_dir, board)
    if now_before != before or now_board_hash != plan["project"]["base_board_hash"]:
        _add_plan_rejection(plan, "stale_revision", "project",
                            "project changed after planning; make a new plan")
        _write_json(plan_path, plan)
        result = _result("rejected", plan, now_before, None, 0, board,
                         "project changed after planning; make a new plan")
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 3, "plan": plan, "result": result, "pipeline": None}

    if not project_dir.is_dir() or not board.is_file():
        reason = "project or board does not exist"
        result = _result("unavailable", plan, before, None, 0, board, reason)
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 3, "plan": plan, "result": result, "pipeline": None}
    if not history.repository_dir:
        reason = "project has no Git history"
        result = _result("unavailable", plan, before, None, 0, board, reason)
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 3, "plan": plan, "result": result, "pipeline": None}
    if _project_dirty(history):
        reason = "project working tree is dirty"
        result = _result("unavailable", plan, before, None, 0, board, reason)
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 3, "plan": plan, "result": result, "pipeline": None}
    cli = _kicad_cli()
    if cli is None:
        reason = "PhiCad kicad-cli is unavailable; set PHICAD_KICAD_CLI"
        result = _result("unavailable", plan, before, None, 0, board, reason)
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 3, "plan": plan, "result": result, "pipeline": None}

    stage: Optional[Path] = None
    backups: Dict[Path, Optional[bytes]] = {}
    changed: List[str] = []
    pipeline_report: Optional[Dict[str, Any]] = None
    try:
        stage, pipeline_report = _pipeline(project_dir, translated, cli)
        # translate() names the design after the target stem, so the staged files already carry
        # it; a mismatch here is a bug worth failing on, not silently renaming around.
        target_stem = board.stem
        stage_files = _native_stage_files(stage, target_stem)
        changed, backups = _copy_transaction(project_dir, stage_files, target_stem, history)
        gitignore_changed = _ensure_journal_ignored(project_dir, backups)
        if gitignore_changed:
            changed.append(gitignore_changed)
        if not changed:
            raise ApplyFailure("rejected", "synth output is byte-identical to the project", "no_change")

        # Snapshot only native outputs.  The synth's temporary repository is never the target
        # history, and calling ensure()/prepare_generated_project here would create a second
        # revision, so an existing project repository is a hard precondition above.
        count_before = _commit_count(history)
        snapshot = history.snapshot(
            "phicad(apply): materialize Circuit JSON",
            files=changed,
            actor="apply",
            meta={"tool": "circuit_json_apply", "plan-hash": plan["plan_hash"]},
        )
        if not snapshot.get("committed"):
            raise ApplyFailure("unavailable", f"project snapshot did not commit: {snapshot.get('reason')}",
                               "history_commit")
        after = _head(history)
        count_after = _commit_count(history)
        if not after or count_before is None or count_after != count_before + 1:
            raise ApplyFailure("unavailable", "project history did not advance exactly once", "history_commit")

        journal_before = history.journal.count()
        journal = history.record(
            "apply", "circuit_json_apply", args={"plan_hash": plan["plan_hash"]},
            files=sorted(set(changed)), commit=after,
            result={"status": "applied", "undo_entries": 1},
        )
        if not journal.get("ok") or history.journal.count() != journal_before + 1:
            # This is a capability failure after the commit, not a success claim.  It is surfaced
            # with the commit hash so an operator can repair the journal rather than losing audit.
            raise ApplyFailure("unavailable", "project history journal could not record one undo entry",
                               "history_journal")

        result = _result("applied", plan, before, after, 1, board, None)
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 0, "plan": plan, "result": result, "pipeline": pipeline_report}
    except ApplyFailure as exc:
        if changed and backups:
            _restore_transaction(backups)
            # A failed snapshot can leave the index staged.  Clear only the files this transaction
            # touched; do not reset unrelated user state.
            history._git(["reset", "--"] + changed, timeout=20.0)
        _add_plan_rejection(plan, exc.code, "project", exc.reason)
        _write_json(plan_path, plan)
        result = _result(exc.status, plan, before, _head(history), 0, board, exc.reason)
        if result_path:
            _write_json(result_path, result)
        return {"exit_code": 3, "plan": plan, "result": result, "pipeline": pipeline_report}
    finally:
        if stage is not None:
            temporary_root = stage.parent
            # `_pipeline` leaves a private temp root in its report; the stage itself is native/.
            shutil.rmtree(temporary_root, ignore_errors=True)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--circuit-json", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--result")
    parser.add_argument("--expected-revision")
    parser.add_argument("--commit", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        output = apply(Path(args.circuit_json).expanduser().resolve(), args.project,
                       Path(args.plan).expanduser().resolve(),
                       Path(args.result).expanduser().resolve() if args.result else None,
                       args.expected_revision, args.commit)
    except ApplyFailure as exc:
        # A plan may not be writable, so this path is intentionally stdout-only.  The JS service
        # still returns a structured unavailable result instead of turning it into a crash.
        output = {"exit_code": 3, "plan": None,
                  "result": {"schema": SCHEMA_RESULT, "status": exc.status,
                             "plan_hash": None,
                             "revision": {"before": None, "after": None, "undo_entries": 0},
                             "board": {"path": str(Path(args.project).resolve()), "sha256": None},
                             "reason": exc.reason}, "pipeline": None}
    except Exception as exc:  # noqa: BLE001 - service boundary must return JSON
        output = {"exit_code": 3, "plan": None,
                  "result": {"schema": SCHEMA_RESULT, "status": "unavailable",
                             "plan_hash": None,
                             "revision": {"before": None, "after": None, "undo_entries": 0},
                             "board": {"path": str(Path(args.project).resolve()), "sha256": None},
                             "reason": f"{type(exc).__name__}: {exc}"}, "pipeline": None}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return int(output.get("exit_code", 3))


if __name__ == "__main__":
    raise SystemExit(main())
