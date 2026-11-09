#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""PhiCad Verify, exposed as MCP tools over the same core the CLI uses.

WHY THESE TOOLS SHELL OUT TO THE SAME CLI THE HUMAN RUNS
--------------------------------------------------------
There is exactly one implementation of the check (``phicad/core/cli.mjs``) and every surface is a
client of it.  Reimplementing the evaluation here in Python -- or worse, calling the tscircuit
packages a second way -- would give an agent a subtly different answer from the one a human gets
from the terminal, which is the failure this fork keeps finding: two paths that are supposed to
agree and quietly do not.

So these tools add no logic of their own beyond argument validation and JSON transport.

WHAT THESE TOOLS DELIBERATELY DO NOT DO
---------------------------------------
* No model calls, no provider configuration, no credentials.  **phi is the only AI
  harness**; PhiCad Verify is a tool Phi calls, never a place where another agent runtime lives.
* No claim about fabrication readiness.  ``verify_build`` returns the tscircuit diagnostics and
  the KiCad CAD findings as two separate result sets with their own verdicts, because a tool's
  opinion of its own output is not an independent check and merging them would imply otherwise.
* No network assumption.  The child process gets an allowlisted environment; the engine runs from
  the committed lockfile's local binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# Resolved the same way the rest of the server resolves paths, so a worktree that sets
# PHICAD_REPO gets its own core rather than another checkout's.
_MCP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(os.environ.get("PHICAD_REPO") or _MCP_DIR.parent.parent)
CORE_DIR = _REPO_ROOT / "phicad" / "core"
CORE_CLI = CORE_DIR / "cli.mjs"

# The runtime is pinned to an absolute path for the same reason the core pins it: PATH order must
# not decide which interpreter runs a build.
_BUN_CANDIDATES = (
    os.environ.get("PHICAD_BUN"),
    "/opt/homebrew/bin/bun",
    "/usr/local/bin/bun",
)

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800
STDOUT_LIMIT = 4 * 1024 * 1024


def _bun() -> str | None:
    for candidate in _BUN_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return shutil.which("bun")


def _env() -> dict[str, str]:
    """An allowlisted environment: no tokens, no keys, no proxy pointing at the model gateway.

    A denylist would be wrong here -- the set of secret-shaped variables on a developer machine is
    open-ended, and one that is forgotten is handed to a third-party build tool.
    """
    allow = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "USER", "SHELL")
    env = {key: os.environ[key] for key in allow if key in os.environ}
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    # Passed through when set: they select which build tree the CAD check uses.
    for key in ("PHICAD_KICAD_CLI", "PHICAD_BUILD", "PHICAD_BUN"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _preflight() -> dict[str, Any] | None:
    if not CORE_CLI.is_file():
        return {
            "ok": False,
            "error": f"PhiCad Verify core not found at {CORE_CLI}",
            "fix": "check PHICAD_REPO, or run `bun install` in phicad/core",
        }
    if _bun() is None:
        return {
            "ok": False,
            "error": "the bun runtime was not found",
            "fix": "install bun, or set PHICAD_BUN to its absolute path",
        }
    if not (CORE_DIR / "node_modules" / ".bin" / "tsci").exists():
        return {
            "ok": False,
            "error": "the tscircuit CLI is not installed in phicad/core/node_modules",
            "fix": "run `bun install` in phicad/core (the lockfile is committed)",
        }
    return None


def _run_core(args: list[str], timeout: int) -> dict[str, Any]:
    """Run the core CLI and return its JSON, or a structured failure. Never raises."""
    bun = _bun()
    assert bun is not None  # _preflight() ran first
    argv = [bun, str(CORE_CLI), *args, "--json"]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env(),
            cwd=str(CORE_DIR),
        )
    except subprocess.TimeoutExpired:
        # A timeout is a real outcome with its own code, not an exception the caller has to guess
        # at, and it is never reported as a passing check.
        return {
            "ok": False,
            "timed_out": True,
            "timeout_s": timeout,
            "error": f"PhiCad Verify exceeded {timeout} s and was terminated",
            "fix": "raise timeout_s, or simplify the design",
            "command": " ".join(argv),
        }
    except OSError as exc:
        return {"ok": False, "error": f"cannot run the core: {exc}", "command": " ".join(argv)}

    elapsed = round(time.monotonic() - started, 3)
    out = (proc.stdout or "")[:STDOUT_LIMIT]
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        return {
            "ok": False,
            "error": "the core produced no JSON",
            "exit_code": proc.returncode,
            "detail": " / ".join(tail),
            "duration_s": elapsed,
            "command": " ".join(argv),
        }
    payload["duration_s"] = elapsed
    # The core's exit code is reported but never used as the verdict: `check` exits non-zero for a
    # design with errors, which is information about the DESIGN, not about whether the tool ran.
    payload["exit_code"] = proc.returncode
    return payload


def _clamp_timeout(value: int | None) -> int:
    if not value or value <= 0:
        return DEFAULT_TIMEOUT
    return min(int(value), MAX_TIMEOUT)


# --------------------------------------------------------------------------- tools

def register(mcp) -> None:
    """Attach the PhiCad Verify tools to an existing FastMCP instance."""

    @mcp.tool
    def verify_capabilities() -> dict[str, Any]:
        """What PhiCad Verify can do on THIS machine, with tool versions. No side effects.

        Call this first. Every field is a checked fact rather than a claim: whether the pinned
        tscircuit engine is installed, whether this fork's kicad-cli was found and at what
        version, and the locked dependency versions that will produce any artifact.

        PhiCad Verify contains no model runtime, no provider UI and no credentials -- phi
        is the only AI harness. The KiCad step is an independent CAD check, not a
        fabrication-readiness verdict.
        """
        if (err := _preflight()):
            return err
        return _run_core(["capabilities"], DEFAULT_TIMEOUT)

    @mcp.tool
    def verify_check(source_path: str, timeout_s: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Evaluate a tscircuit TSX design and return its diagnostics. No CAD check.

        This is the fast inner loop for an agent: it runs the pinned engine over `source_path`
        and returns the design census plus tscircuit's own typed diagnostics, translated into
        PhiCad findings with stable ids.

        A finding's `id` is derived from its code, refs and rounded position -- not its wording --
        so the same problem keeps the same id across runs and a waiver stays granted.
        """
        if (err := _preflight()):
            return err
        src = Path(source_path).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"no such file: {src}"}
        if src.suffix not in (".tsx", ".ts"):
            return {"ok": False, "error": f"expected a .tsx or .ts design, got {src.suffix}"}
        return _run_core(["check", str(src.resolve())], _clamp_timeout(timeout_s))

    @mcp.tool
    def verify_build(source_path: str, timeout_s: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Build a TSX design, then check the exported board with KiCad's DRC.

        Returns TWO independent result sets that are never merged:

          `engine`       -- the tscircuit build and its own diagnostics
          `verification` -- this fork's kicad-cli reading the exported .kicad_pcb

        The engine reporting success is not evidence the board is sound; that is the whole reason
        the second check exists and is reported separately. `verification.skipped` is true when
        kicad-cli is absent, and is NEVER a pass -- a checker that could not run is not a checker
        that was satisfied.

        The KiCad step evaluates the board against the design rules recorded in the exported file.
        It is an independent CAD check, not a fabrication-readiness verdict; no fabricator's
        process or acceptance criteria are consulted. It is also not a manufacturing export -- that
        is `kicad-cli pcb export` in the same native PhiCad application.
        """
        if (err := _preflight()):
            return err
        src = Path(source_path).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"no such file: {src}"}
        if src.suffix not in (".tsx", ".ts"):
            return {"ok": False, "error": f"expected a .tsx or .ts design, got {src.suffix}"}
        return _run_core(["build", str(src.resolve())], _clamp_timeout(timeout_s))

    @mcp.tool
    def verify_circuit_json(circuit_json_path: str, output_dir: str, result_path: str = "",
                            timeout_s: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Check a SUPPLIED Circuit JSON with the independent KiCad path. No TSX re-evaluation.

        This is the Platform service operation. Use it when you already hold the Circuit JSON and
        want it checked -- as opposed to `verify_build`, which evaluates a `.tsx` source first.

        The difference matters and is not cosmetic. Re-evaluating a source is not guaranteed to
        reproduce the artifact you are holding: the engine mints random element ids and orders
        diagnostics by whichever supplier lookup finished first. Checking a board the caller never
        saw, then reporting it as the answer to their question, is a confident wrong answer. So this
        operation exports **the document you pass** and never reads a TSX file (`source_hash` is
        always null).

        The caller's FILENAME is not part of the contract: any regular `.json` path works. (The
        pinned engine detects Circuit JSON by basename, so the service stages a private copy inside
        `output_dir` and removes it before enumerating artifacts. `circuit_json_hash` is always the
        hash of the original bytes you supplied, never of the copy.)

        Result shape (service contract v1, stable): `artifacts` is a list of paths relative to
        `output_dir`; `artifact_hashes` is keyed by those same relative paths; `tool_versions` holds
        exact scalar version strings only (`tscircuit`, `@tscircuit/eval`, `circuit-json`,
        `kicad-cli`, `bun`, `service-contract`) for a machine-verifiable version lock, while
        resolved binary paths live in `tool_paths` -- a path is not a version. `result_path` may
        live inside `output_dir` and is never listed as an artifact of the run that produced it.

        `output_dir` must be FRESH. A directory that already holds artifacts is refused
        (`verify.output_dir_not_fresh`) rather than mixed in, because a stale file in the result
        would make the artifact manifest a lie. Nothing is written outside it, and every artifact
        path in the result is relative to it with a sha256 in `artifact_hashes`.

        Diagnostic ORIGINS are preserved, never merged, and there are EXACTLY TWO: `kicad` for what
        this checker independently produced, and `tscircuit` for a diagnostic found inside the
        supplied document and relayed unchanged. The enum is CLOSED -- any other value, including the
        retired `openpcb` lane, fails closed as `verify.invalid_origin`, because accepting a value is
        itself a contract and a consumer must not be handed an origin this service can never justify.
        Origins are assigned here, never read from the document, so an element carrying its own
        `origin` cannot inject one. `ok` reflects the independent KiCad check only.

        The KiCad step is an independent CAD check against the design rules recorded in the exported
        board -- not a fabrication-readiness verdict.
        """
        if (err := _preflight()):
            return err
        src = Path(circuit_json_path).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"no such Circuit JSON: {src}"}
        if src.suffix != ".json":
            return {"ok": False, "error": f"expected a .json Circuit JSON document, got {src.suffix}"}
        if not output_dir:
            return {"ok": False, "error": "output_dir is required and must be a fresh directory"}
        argv = ["verify", "--circuit-json", str(src.resolve()),
                "--output-dir", str(Path(output_dir).expanduser().resolve())]
        if result_path:
            argv += ["--result", str(Path(result_path).expanduser().resolve())]
        return _run_core(argv, _clamp_timeout(timeout_s))

    @mcp.tool
    def verify_launch_targets() -> dict[str, Any]:
        """Which other product views a project can be handed to, and whether they resolve here.

        Returns the versioned launch contract and one entry per view: `studio` (PhiCad
        Studio), `source` (the authored tscircuit code), `phi` (a phi session) and `docs`
        (the platform documentation).

        `resolvable` is a checked fact about THIS machine, not an aspiration. A view with no
        installed application and no configured platform launcher reports `resolvable: false` with
        a reason, because a deep link that silently fails is worse than one that admits it cannot
        resolve. Configure with PHICAD_STUDIO, PHICAD_PLATFORM_LAUNCHER and PHICAD_PLATFORM_DOCS.
        """
        if (err := _preflight()):
            return err
        return _run_core(["capabilities"], DEFAULT_TIMEOUT)

    @mcp.tool
    def verify_open_view(source_path: str, view: str = "source",
                         timeout_s: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Emit a manifest-bound launch request so another view can open the SAME revision.

        `view` is one of `studio`, `source`, `phi`, `docs`.

        This EMITS a request and opens nothing. PhiCad Verify does not own the other applications
        and must not mutate them: the PhiCad Platform launcher decides whether and how to honour a
        request. No duplicate desktop shell is involved, and no application is launched from here.

        The request carries project identity, manifest identity, the expected content hash and the
        desired view -- and **never credentials** (`carriesCredentials: false`, asserted by a test
        over the serialised payload). A receiver is expected to reject a stale or mismatched hash;
        Verify never reconciles revisions and never does last-writer-wins, because silently picking
        a revision for someone is how two tools come to disagree about what the board is.

        The returned `target.resolvable` tells you whether the requested view actually exists on
        this machine. A request for an unresolvable view is still returned, so a caller can show
        the intended navigation honestly rather than a dead link.
        """
        if (err := _preflight()):
            return err
        src = Path(source_path).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"no such file: {src}"}
        if src.suffix not in (".tsx", ".ts"):
            return {"ok": False, "error": f"expected a .tsx or .ts design, got {src.suffix}"}
        known = ("studio", "source", "phi", "docs")
        if view not in known:
            return {"ok": False, "error": f"unknown view {view!r}; known views: {', '.join(known)}"}
        return _run_core(["open", str(src.resolve()), "--view", view], _clamp_timeout(timeout_s))

    @mcp.tool
    def verify_manifest(source_path: str, timeout_s: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Build a design and return the artifact manifest: files, digests and tool provenance.

        Use this when the answer has to be auditable. Each artifact carries its size, a raw
        sha256, and a content digest computed with the engine's generated element ids removed --
        tscircuit mints a random nanoid per diagnostic, so two builds of one unchanged source
        differ byte-for-byte while describing the identical design.

        `manifestId` is deterministic for a given source and toolchain: it is derived from the
        source digest, the pinned tool versions and the sorted artifact content digests, and
        excludes paths, timestamps and durations. Two runs of the same design agree; any real
        change to the design or the toolchain changes it.

        `boundary` records that Circuit JSON is a versioned build/interchange artifact rather than
        authored source -- it is not lossless and is not intended to be round-tripped back to TSX.
        """
        if (err := _preflight()):
            return err
        src = Path(source_path).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"no such file: {src}"}
        if src.suffix not in (".tsx", ".ts"):
            return {"ok": False, "error": f"expected a .tsx or .ts design, got {src.suffix}"}
        return _run_core(["manifest", str(src.resolve())], _clamp_timeout(timeout_s))
