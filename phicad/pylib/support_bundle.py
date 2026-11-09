#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <http://www.gnu.org/licenses/>.

"""Create a local, privacy-safe PhiCad support bundle.

This module is intentionally standalone.  It does not import the PhiCad CLI, inspect
Phi's state, or invoke a command supplied by the caller.  The archive contains facts
and bounded command *outcomes*, not command transcripts.  If a project is explicitly
selected, its file names, sizes, and SHA-256 digests are recorded; project bytes are
never written to the archive.

The small amount of command execution here is deliberately boring.  Every command is
listed in ``_COMMAND_SPECS``, is launched without a shell, receives a reduced
environment, has a timeout and bounded pipe capture, and contributes only a status and
(strictly parsed) version to the bundle.  In particular, this file must never be
changed to call a credential broker or to read an auth/configuration directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
BUNDLE_TYPE = "phicad-support-bundle"
COMMAND_TIMEOUT_SECONDS = 5.0
COMMAND_OUTPUT_LIMIT = 32 * 1024
PROJECT_FILE_LIMIT = 2048
PROJECT_DIRECTORY_LIMIT = 2048
PROJECT_ENTRY_LIMIT = 8192
PROJECT_ROOT_ENTRY_LIMIT = 512
PROJECT_BYTE_LIMIT = 256 * 1024 * 1024
_PROJECT_FILE_SUFFIXES = {".kicad_pro", ".kicad_pcb", ".kicad_sch", ".json"}
_PROJECT_DIRECTORY_MARKERS = {".kicad_pro", ".kicad_pcb", ".kicad_sch"}

# Only these names may be read when constructing the child environment or checking
# configured locations.  Values are never placed in a bundle.  The first group is
# enough to run the fixed probes; the PHICAD names are presence-only configuration
# facts used by the build/doctor sections.
ALLOWED_ENV_NAMES = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "USERNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "PHICAD_REPO",
    "PHICAD_BUILD",
    "PHICAD_KICAD_CLI",
    "PHICAD_VENV",
    "PHICAD_MCP_VENV",
    "KICAD_RUN_FROM_BUILD_DIR",
)
_CHILD_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_PATH_ENV_NAMES = (
    "PHICAD_REPO",
    "PHICAD_BUILD",
    "PHICAD_KICAD_CLI",
    "PHICAD_VENV",
    "PHICAD_MCP_VENV",
)

# These are identifiers, not user-configurable commands.  A command's stdout/stderr
# is never copied into an archive.  The output is used only to recognize a plain
# numeric version, when one is available.
_COMMAND_SPECS = (
    ("python_version", "python", ("--version",)),
    ("git_version", "git", ("--version",)),
    ("cmake_version", "cmake", ("--version",)),
    ("ninja_version", "ninja", ("--version",)),
    ("clang_version", "clang", ("--version",)),
    ("git_repository", "git", ("rev-parse", "--show-toplevel")),
    ("git_branch", "git", ("branch", "--show-current")),
    ("git_dirty", "git", ("status", "--porcelain", "--untracked-files=normal")),
    ("git_commit", "git", ("rev-parse", "--short=12", "HEAD")),
    (
        "git_release",
        "git",
        ("describe", "--tags", "--exact-match", "--match", "exo-v*", "--match", "phicad-v*", "HEAD"),
    ),
)

_README = """PhiCad local support bundle
============================

This archive was created locally by PhiCad. It is intended for a maintainer to
inspect the health of the application without receiving the user's design or
private configuration.

Privacy guarantees
------------------
* No board, schematic, project, transcript, prompt, log, browser-storage, or
  credential contents are included.
* No environment values, usernames, home paths, tokens, passwords, API keys,
  or URLs containing credentials/query data are included.
* Command output is not included. Commands contribute only bounded status
  facts and, when unambiguous, a numeric version or source identifier.
* If project metadata was requested explicitly, it contains only relative file
  names, byte sizes, and SHA-256 digests. The digest is not a copy of the file.
  Credential-like files and protected state directories are skipped.

The complete archive format is described by manifest.json. The schema version
is intentionally small and can be checked before processing the other files.
"""

# Home/user/path values are removed before any user-derived string is serialized.
# The extra words catch the conventional names used by secret-bearing paths and
# make canary-style tests useful without trying to guess arbitrary user secrets.
_SECRET_WORDS = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bearer|credential|"
    r"password|passwd|secret|private[_-]?key|canary)"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"secret|token|credential|authorization|private[_-]?key|"
    r"[A-Za-z][A-Za-z0-9_.-]*(?:token|secret|password|passwd|credential))\b"
    r"\s*[:=]\s*)([\"']?[^\s,;&\"']+[\"']?)"
)
_URL = re.compile(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]{1,15}://[^\s<>\"']+")
_AUTH_HEADER = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*"
    r"(?:basic|digest|bearer)\s+[^\s,;]+"
)
_TOKEN_SHAPES = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{12,})\b"
)
_PEM = re.compile(
    r"-----BEGIN [^-\r\n]{1,80}-----.*?-----END [^-\r\n]{1,80}-----",
    re.IGNORECASE | re.DOTALL,
)

_PROTECTED_COMPONENTS = {
    ".git",
    ".pi",
    ".phi",
    "transcripts",
    "prompts",
    "browser-storage",
    "browser_storage",
    "cookies",
    "credentials",
    "logs",
    "browser",
    "browserstorage",
    "local_storage",
    "local storage",
}
_SENSITIVE_FILE_NAME = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|.*(?:credential|password|passwd|secret|token|api[_-]?key|"
    r"private[_-]?key).*)$"
)
_SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".kdbx", ".log"}


# --------------------------------------------------------------------------------------
# Redaction and safe serialization
# --------------------------------------------------------------------------------------


def _environment_values() -> List[str]:
    """Return known path/user values for replacement, never for reporting."""
    values: List[str] = []
    for name in ALLOWED_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            values.append(value)
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        home = ""
    if home:
        values.append(home)
    return sorted(set(value for value in values if len(value) >= 2), key=len, reverse=True)


def _redact_urls(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        # A URL without a query or user-info can still be useful as a product endpoint.
        # A path containing a conventional secret name is not useful enough to retain.
        if "?" in value or "#" in value or "@" in value or _SECRET_WORDS.search(value):
            return "<REDACTED_URL>"
        return value

    return _URL.sub(replace, text)


def sanitize_text(value: Any) -> str:
    """Make a string safe for both human text and JSON values.

    This is intentionally applied to every non-constant string at the final boundary,
    not just to subprocess results.  A failed filesystem operation therefore cannot
    accidentally reintroduce a path that an earlier collector forgot to sanitize.
    """
    text = str(value)
    text = _CONTROL_CHARS.sub("?", text)
    for known in _environment_values():
        text = text.replace(known, "<REDACTED_PATH>")
    text = _PEM.sub("<REDACTED_KEY>", text)
    text = _redact_urls(text)
    text = _AUTH_HEADER.sub("Authorization: <REDACTED>", text)
    text = _BEARER.sub("Bearer <REDACTED>", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1<REDACTED>", text)
    text = _TOKEN_SHAPES.sub("<REDACTED_TOKEN>", text)
    # Secret-like words in paths are not useful diagnostic data.  This also
    # protects a user-chosen project filename without needing to know its value.
    text = _SECRET_WORDS.sub("<REDACTED>", text)
    return text


def _sanitize_path_name(value: str) -> str:
    """Sanitize a relative project name without exposing a secret-looking component."""
    value = sanitize_text(value).replace("\\", "/")
    components = []
    for component in value.split("/"):
        if not component or component == ".":
            continue
        if component == "..":
            components.append("<PARENT>")
            continue
        if _SECRET_WORDS.search(component):
            component = _SECRET_WORDS.sub("<REDACTED>", component)
        components.append(component)
    return "/".join(components) or "<ROOT>"


def _sanitize_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_data(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(_sanitize_data(value), indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


# --------------------------------------------------------------------------------------
# Bounded command probes
# --------------------------------------------------------------------------------------


def _child_environment() -> Dict[str, str]:
    """Build the only environment a probe is allowed to inherit."""
    child: Dict[str, str] = {}
    for name in _CHILD_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            child[name] = value
    child["LC_ALL"] = "C"
    return child


def _command_available(executable: str, env: Mapping[str, str]) -> bool:
    if os.path.isabs(executable):
        return os.access(executable, os.X_OK)
    return shutil.which(executable, path=env.get("PATH")) is not None


def run_command(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    output_limit: int = COMMAND_OUTPUT_LIMIT,
) -> Dict[str, Any]:
    """Run one already-whitelisted probe with bounded time and pipe capture.

    The returned mapping deliberately contains no captured output.  Presence flags are
    enough to explain a failed probe, and keeping stderr out of the result avoids the
    common diagnostic failure where a loader error echoes a credential-bearing path.
    """
    child = dict(env) if env is not None else _child_environment()
    stdout = bytearray()
    stderr = bytearray()
    truncated = {"stdout": False, "stderr": False}
    started = time.monotonic()
    process: Optional[subprocess.Popen[bytes]] = None
    selector: Optional[selectors.BaseSelector] = None

    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=child,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        assert process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        timed_out = False

        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in selector.select(min(0.1, remaining)):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                target = stdout if key.data == "stdout" else stderr
                if len(target) < output_limit:
                    room = output_limit - len(target)
                    target.extend(chunk[:room])
                    if len(chunk) > room:
                        truncated[key.data] = True
                else:
                    truncated[key.data] = True

        if timed_out:
            # Killing closes the writer eventually, but wait before unregistering pipes so
            # that no child survives the support-bundle process.
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            status = "timeout"
            return {
                "status": status,
                "returncode": None,
                "stdout_present": bool(stdout),
                "stderr_present": bool(stderr),
                # A timeout prevents us from draining the remaining pipe, so the
                # capture is necessarily incomplete even if its stored prefix was
                # shorter than the byte limit.
                "output_truncated": True,
            }

        returncode = process.wait(timeout=max(0.1, timeout))
        result = {
            "status": "ok" if returncode == 0 else "failed",
            "returncode": returncode,
            "stdout_present": bool(stdout),
            "stderr_present": bool(stderr),
            "output_truncated": bool(truncated["stdout"] or truncated["stderr"]),
        }
        # Keep only a strict numeric version.  The helper is defined below, but is
        # available when this function is called after module initialization.
        version = _numeric_version(bytes(stdout), bytes(stderr))
        if version is not None:
            result["version"] = version
        identifier = _safe_identifier(bytes(stdout))
        if identifier is not None:
            result["identifier"] = identifier
        return result
    except FileNotFoundError:
        return {"status": "missing", "returncode": None, "stdout_present": False, "stderr_present": False}
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=1.0)
            except (OSError, subprocess.SubprocessError):
                pass
        return {"status": "failed", "returncode": None, "stdout_present": False, "stderr_present": False}
    finally:
        if selector is not None:
            try:
                for key in list(selector.get_map().values()):
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, OSError):
                        pass
                    try:
                        key.fileobj.close()
                    except OSError:
                        pass
                selector.close()
            except OSError:
                pass


def _numeric_version(stdout: bytes, stderr: bytes) -> Optional[str]:
    """Accept only a standalone numeric version line; reject arbitrary diagnostics."""
    for raw in (stdout + b"\n" + stderr).splitlines():
        try:
            line = raw.decode("ascii", "strict").strip()
        except UnicodeDecodeError:
            continue
        match = re.fullmatch(r"\d+(?:\.\d+){1,3}(?:-phicad)?", line)
        if match:
            return match.group(0)
        match = re.search(
            r"(?:Python|git version|cmake version|ninja version|clang version)\s+"
            r"(\d+(?:\.\d+){1,3})(?![\d.])",
            line,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
    return None


def _safe_identifier(stdout: bytes) -> Optional[str]:
    """Return only a source hash or PhiCad release tag, never an arbitrary branch/path."""
    try:
        lines = [line.strip() for line in stdout.decode("ascii", "strict").splitlines() if line.strip()]
    except UnicodeDecodeError:
        return None
    if len(lines) != 1:
        return None
    value = lines[0]
    if re.fullmatch(r"[0-9a-f]{12,40}", value):
        return value
    if re.fullmatch(r"(?:exo|phicad)-v[0-9A-Za-z][0-9A-Za-z._+-]{0,63}", value):
        return value
    return None


def _run_probe(
    command_id: str,
    executable: str,
    arguments: Sequence[str],
    *,
    cwd: Optional[Path],
) -> Dict[str, Any]:
    env = _child_environment()
    result = {
        "id": command_id,
        # Never put an interpreter or configured binary path in the bundle.
        "executable": command_id.rsplit("_", 1)[0] if command_id.endswith("_version") else executable,
        "arguments": list(arguments),
        "status": "missing",
        "returncode": None,
        "stdout_present": False,
        "stderr_present": False,
    }
    if not _command_available(executable, env):
        return result

    outcome = run_command([executable, *arguments], cwd=cwd, env=env)
    result.update(outcome)
    # A second execution to retrieve a version would defeat the timeout/output policy.
    # The runner retains only the strict numeric version field, never a raw line.
    if not command_id.endswith("_version"):
        result.pop("version", None)
    if command_id not in ("git_commit", "git_release"):
        result.pop("identifier", None)
    return result


def collect_command_outcomes(cwd: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Run the fixed probe list and return status-only command outcomes."""
    workdir = cwd or Path.cwd()
    source_root = _source_root()
    outcomes = []
    for command_id, executable, arguments in _COMMAND_SPECS:
        # The Python executable is resolved once here, but its path is never reported.
        if executable == "python":
            executable = sys.executable or "python3"
        probe_cwd = source_root if command_id.startswith("git_") and command_id != "git_version" else workdir
        outcomes.append(_run_probe(command_id, executable, arguments, cwd=probe_cwd))

    # Never execute a user-configured path from PHICAD_KICAD_CLI in a diagnostic collector.
    # The fixed default build-tree path is safe to probe and gives a native product version when
    # this checkout owns a build; an external build remains presence-only configuration data.
    import build_layout  # sibling module in phicad/pylib
    native_cli = build_layout.kicad_cli_in(build_layout.default_build_dir(source_root))
    outcomes.append(_run_probe("phicad_native_version", str(native_cli), ("version",), cwd=source_root))
    return outcomes


# --------------------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------------------


def _source_root() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, RuntimeError, IndexError):
        return Path.cwd()


def _is_forbidden_path(path: Path) -> bool:
    """Avoid even traversing known private state locations."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path.absolute()
    parts = {part.lower() for part in resolved.parts}
    if ".pi" in parts or "transcripts" in parts or "prompts" in parts:
        return True
    try:
        home = Path.home().resolve()
        private = home / ".pi"
        return resolved == private or private in resolved.parents
    except (OSError, RuntimeError):
        return False


def _env_presence() -> Dict[str, str]:
    reported = (*_PATH_ENV_NAMES, "KICAD_RUN_FROM_BUILD_DIR")
    return {name: ("set" if os.environ.get(name) else "unset") for name in reported}


def _configured_build_dir(source_root: Path) -> Tuple[Optional[Path], str]:
    raw = os.environ.get("PHICAD_BUILD")
    if raw:
        candidate = Path(raw).expanduser()
        return candidate, "PHICAD_BUILD"
    return source_root / "build" / "mac-dev", "default"


def _build_facts(source_root: Path) -> Dict[str, Any]:
    build_dir, source = _configured_build_dir(source_root)
    if build_dir is None or _is_forbidden_path(build_dir):
        present = False
        configured = False
        binary_present = {"kicad_cli": False, "phicad_gui": False}
    else:
        try:
            present = build_dir.is_dir()
            configured = (build_dir / "build.ninja").is_file()
            binary_paths = (
                build_dir / "kicad" / "PhiCad.app" / "Contents" / "MacOS",
                build_dir / "kicad",
            )
            binary_present = {
                "kicad_cli": any((directory / "kicad-cli").is_file() for directory in binary_paths),
                "phicad_gui": any((directory / "PhiCad").is_file() for directory in binary_paths),
            }
        except OSError:
            present = False
            configured = False
            binary_present = {"kicad_cli": False, "phicad_gui": False}
    return {
        "build_directory": {"status": "present" if present else "missing", "source": source},
        "configured": configured,
        "binaries": binary_present,
        "configured_overrides": {name: ("set" if os.environ.get(name) else "unset") for name in _PATH_ENV_NAMES},
    }


def _status_by_id(outcomes: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {str(item.get("id")): item for item in outcomes}


def collect_facts(outcomes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    source_root = _source_root()
    by_id = _status_by_id(outcomes)
    git_available = _command_available("git", _child_environment())
    build = _build_facts(source_root)
    dirty_probe = by_id.get("git_dirty", {})
    source_dirty = dirty_probe.get("stdout_present", False) if dirty_probe.get("status") == "ok" else None
    build_checks = [
        {"name": "python", "status": by_id.get("python_version", {}).get("status", "unknown")},
        {"name": "git", "status": "available" if git_available else "missing"},
        {"name": "build_tree", "status": build["build_directory"]["status"]},
        {"name": "build_configured", "status": "yes" if build["configured"] else "no"},
        {
            "name": "kicad_cli",
            "status": "present" if build["binaries"]["kicad_cli"] else "missing",
        },
    ]
    return {
        "system": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
        "app": {
            "name": "PhiCad",
            "support_bundle": "available",
            "source_tree": "present" if (source_root / "phicad").is_dir() else "missing",
            "release": by_id.get("git_release", {}).get("identifier", "unreleased"),
            "source_commit": by_id.get("git_commit", {}).get("identifier", "unknown"),
            "source_dirty": source_dirty,
            "native_version": by_id.get("phicad_native_version", {}).get("version"),
            "configuration_presence": _env_presence(),
        },
        "git": {
            "available": git_available,
            "repository": "detected" if by_id.get("git_repository", {}).get("status") == "ok" else "not_detected",
            "branch": (
                "attached"
                if by_id.get("git_branch", {}).get("status") == "ok"
                and by_id.get("git_branch", {}).get("stdout_present")
                else "detached_or_unknown"
            ),
        },
        "build": build,
        "doctor": {"checks": build_checks},
    }


# --------------------------------------------------------------------------------------
# Explicit project metadata
# --------------------------------------------------------------------------------------


def _skip_project_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower in _PROTECTED_COMPONENTS
        or _SENSITIVE_FILE_NAME.fullmatch(name) is not None
        or Path(name).suffix.lower() in _SENSITIVE_SUFFIXES
    )


def _hash_file(path: Path) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def _looks_like_project(root: Path) -> bool:
    if root.is_file():
        return root.suffix.lower() in _PROJECT_FILE_SUFFIXES
    if not root.is_dir():
        return False
    try:
        for index, child in enumerate(root.iterdir()):
            if index >= PROJECT_ROOT_ENTRY_LIMIT:
                break
            if child.is_file() and child.suffix.lower() in _PROJECT_DIRECTORY_MARKERS:
                return True
    except OSError:
        return False
    return False


def collect_project_metadata(project: Path, *, exclude: Optional[Path] = None) -> Dict[str, Any]:
    """Collect metadata from an explicitly named, recognizable project, never file bytes."""
    result: Dict[str, Any] = {
        "requested": True,
        "status": "unavailable",
        "files": [],
        "limits": {
            "files": PROJECT_FILE_LIMIT,
            "directories": PROJECT_DIRECTORY_LIMIT,
            "entries": PROJECT_ENTRY_LIMIT,
            "root_entries_checked": PROJECT_ROOT_ENTRY_LIMIT,
            "bytes_hashed": PROJECT_BYTE_LIMIT,
        },
    }
    try:
        root = project.expanduser()
        if root.is_symlink():
            result["status"] = "blocked"
            return result
        if _is_forbidden_path(root) or not root.exists():
            result["status"] = "blocked" if _is_forbidden_path(root) else "missing"
            return result
        root = root.resolve()
        anchor = Path(root.anchor)
        home = Path.home().resolve()
        if root == anchor or root == home or not _looks_like_project(root):
            result["status"] = "blocked"
            return result
    except (OSError, RuntimeError):
        result["status"] = "unavailable"
        return result

    excluded: Optional[Path] = None
    if exclude is not None:
        try:
            excluded = exclude.resolve()
        except (OSError, RuntimeError):
            excluded = exclude.absolute()
    files: List[Dict[str, Any]] = []
    total_size = 0
    skipped = 0
    directories_seen = 0
    entries_seen = 0

    def add_file(path: Path, relative: str) -> None:
        nonlocal total_size, skipped
        if len(files) >= PROJECT_FILE_LIMIT:
            skipped += 1
            return
        if excluded is not None and path == excluded:
            return
        if _skip_project_name(path.name):
            skipped += 1
            return
        try:
            stat = path.stat()
            if not path.is_file() or path.is_symlink():
                skipped += 1
                return
            size = int(stat.st_size)
        except OSError:
            skipped += 1
            return
        if total_size + size > PROJECT_BYTE_LIMIT:
            skipped += 1
            return
        digest = _hash_file(path)
        if digest is None:
            skipped += 1
            return
        files.append({"name": _sanitize_path_name(relative), "size": size, "sha256": digest})
        total_size += size

    try:
        if root.is_file() and not root.is_symlink():
            add_file(root, root.name)
        elif root.is_dir():
            for directory, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
                directories_seen += 1
                if directories_seen > PROJECT_DIRECTORY_LIMIT:
                    skipped += 1
                    break
                current = Path(directory)
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if not _skip_project_name(name) and name.lower() not in _PROTECTED_COMPONENTS
                )
                for filename in sorted(filenames):
                    entries_seen += 1
                    if entries_seen > PROJECT_ENTRY_LIMIT:
                        skipped += 1
                        break
                    path = current / filename
                    try:
                        relative = path.relative_to(root).as_posix()
                    except ValueError:
                        skipped += 1
                        continue
                    add_file(path, relative)
                    if len(files) >= PROJECT_FILE_LIMIT:
                        break
                if len(files) >= PROJECT_FILE_LIMIT or entries_seen > PROJECT_ENTRY_LIMIT:
                    break
    except (OSError, RuntimeError):
        result["status"] = "partial"
    else:
        result["status"] = "ok"

    result["files"] = sorted(files, key=lambda item: item["name"])
    result["file_count"] = len(files)
    result["total_size"] = total_size
    result["skipped_files"] = skipped
    return result


# --------------------------------------------------------------------------------------
# Archive assembly and CLI
# --------------------------------------------------------------------------------------


def _default_output(directory: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    candidate = directory / ("phicad-support-bundle-" + stamp + ".zip")
    suffix = 1
    while candidate.exists():
        candidate = directory / ("phicad-support-bundle-%s-%d.zip" % (stamp, suffix))
        suffix += 1
    return candidate


def _write_archive(output: Path, members: Mapping[str, bytes]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(output), mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, members[name])
    output.chmod(0o600)


def create_bundle(
    output: Optional[Path] = None,
    *,
    project: Optional[Path] = None,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create an archive and return the same safe summary used by ``--json``."""
    workdir = (cwd or Path.cwd()).resolve()
    destination = Path(output).expanduser() if output is not None else _default_output(workdir)
    destination = destination if destination.is_absolute() else workdir / destination
    outcomes = collect_command_outcomes(workdir)
    facts = collect_facts(outcomes)
    project_metadata = collect_project_metadata(Path(project), exclude=destination) if project is not None else None

    members: Dict[str, bytes] = {
        "README.txt": _README.encode("utf-8"),
        "system.json": _json_bytes(facts["system"]),
        "app.json": _json_bytes(facts["app"]),
        "git.json": _json_bytes(facts["git"]),
        "build.json": _json_bytes(facts["build"]),
        "doctor.json": _json_bytes(facts["doctor"]),
        "commands.json": _json_bytes({"commands": outcomes}),
    }
    if project_metadata is not None:
        members["project.json"] = _json_bytes(project_metadata)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": BUNDLE_TYPE,
        "privacy": {
            "project_contents_included": False,
            "environment_values_included": False,
            "command_output_included": False,
            "credentials_included": False,
            "redaction": "home/user/path, credential patterns, control characters",
        },
        "members": sorted([*members, "manifest.json"]),
        "project_metadata": project_metadata is not None,
        "project_metadata_member": "project.json" if project_metadata is not None else None,
    }
    members["manifest.json"] = _json_bytes(manifest)
    _write_archive(destination, members)
    archive_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "output": sanitize_text(destination.name),
        "sha256": archive_sha256,
        "manifest": manifest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a local privacy-safe PhiCad support bundle")
    parser.add_argument("--output", type=Path, help="zip path (default: timestamped archive in the current directory)")
    parser.add_argument("--project", type=Path, help="explicit project path; include metadata only, never contents")
    parser.add_argument("--json", action="store_true", help="print a machine-readable result")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = create_bundle(args.output, project=args.project)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        # Do not echo the exception: filesystem errors commonly contain the requested path.
        message = {"status": "error", "error": type(exc).__name__}
        if args.json:
            print(json.dumps(message, sort_keys=True))
        else:
            print("Unable to create support bundle (%s)" % type(exc).__name__, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_sanitize_data(result), indent=2, sort_keys=True))
    else:
        print("Created local support bundle: %s" % result["output"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
