//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The bounded Platform-facing service for Circuit JSON -> native PhiCad materialization.
// The JS side validates the boundary and owns cancellation; the Python side invokes the
// existing synth pipeline and project_history.py through one fixed argument vector.

import { existsSync, lstatSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { ExecError, minimalEnv, run } from "./exec.mjs";

const CORE_DIR = resolve(fileURLToPath(new URL(".", import.meta.url)));
const APPLY_PYTHON = [
  process.env.PHICAD_PYTHON,
  "/usr/bin/python3",
  "/opt/homebrew/bin/python3",
  "/usr/local/bin/python3",
].filter(Boolean).find((candidate) => existsSync(candidate)) ?? null;

export const APPLY_LIMITS = Object.freeze({
  timeoutMs: 60_000,
  terminationGraceMs: 2_000,
  maxSourceBytes: 2 * 1024 * 1024,
  maxJsonBytes: 1024 * 1024,
  maxElements: 100_000,
});

function unavailable(reason, { planHash = null, project = null, board = null } = {}) {
  return {
    exit_code: 3,
    plan: null,
    result: {
      schema: "io.github.couplet-physical-ai.phicad.apply-result/v1",
      status: "unavailable",
      plan_hash: planHash,
      revision: { before: null, after: null, undo_entries: 0 },
      board: { path: board ?? project ?? null, sha256: null },
      reason,
    },
    pipeline: null,
  };
}

function regularNonSymlink(path, label) {
  try {
    const info = lstatSync(path);
    if (info.isSymbolicLink()) return `${label} is a symlink`;
    if (!info.isFile()) return `${label} is not a regular file`;
    return null;
  } catch (err) {
    return `${label} cannot be inspected: ${err.message}`;
  }
}

function projectDirectory(raw) {
  const given = resolve(raw);
  try {
    const info = lstatSync(given);
    if (info.isSymbolicLink()) return given.endsWith(".kicad_pro") ? resolve(join(given, "..")) : given;
    if (info.isFile()) return resolve(join(given, ".."));
    return given;
  } catch {
    return given.endsWith(".kicad_pro") ? resolve(join(given, "..")) : given;
  }
}

function parseChildJson(stdout) {
  const lines = stdout.trim().split("\n").filter(Boolean);
  if (!lines.length) return null;
  try { return JSON.parse(lines.at(-1)); } catch { return null; }
}

/**
 * Run the one service operation.  No shell, no caller-provided executable and no inherited
 * credentials reach the Python process.  The plan/result files are produced by the Python
 * boundary so their canonical serializer is shared by CLI and library callers.
 */
export async function runApply({
  circuitJsonPath,
  projectPath,
  planPath,
  resultPath = null,
  expectedRevision = null,
  commit = false,
  signal = null,
} = {}) {
  if (!circuitJsonPath || !projectPath || !planPath) {
    return unavailable("apply requires --circuit-json, --project and --plan");
  }
  if (!APPLY_PYTHON) return unavailable("Python 3 is unavailable for the apply service");

  const source = resolve(circuitJsonPath);
  const project = resolve(projectPath);
  const plan = resolve(planPath);
  const result = resultPath ? resolve(resultPath) : null;
  const sourceError = regularNonSymlink(source, "Circuit JSON input");
  if (sourceError) return unavailable(sourceError, { project });
  try {
    const bytes = readFileSync(source);
    if (bytes.byteLength > APPLY_LIMITS.maxSourceBytes) {
      return unavailable(`Circuit JSON exceeds ${APPLY_LIMITS.maxSourceBytes} bytes`, { project });
    }
    // Parse only for the limit check.  Python is the authority for exact JSON semantics and
    // exact-byte hashing; this keeps the JS boundary from normalising the caller's bytes.
    const value = JSON.parse(bytes.toString("utf8"));
    if (!Array.isArray(value)) return unavailable("Circuit JSON root must be an array", { project });
    if (value.length > APPLY_LIMITS.maxElements) {
      return unavailable(`Circuit JSON exceeds ${APPLY_LIMITS.maxElements} elements`, { project });
    }
  } catch (err) {
    return unavailable(`cannot read Circuit JSON: ${err.message}`, { project });
  }
  for (const [path, label] of [[plan, "plan output"], ...(result ? [[result, "result output"]] : [])]) {
    try {
      if (lstatSync(path).isSymbolicLink()) return unavailable(`${label} is a symlink`, { project });
    } catch (err) {
      // A new output is fine; an inaccessible existing path is not.
      if (err.code !== "ENOENT") return unavailable(`${label} cannot be inspected: ${err.message}`, { project });
    }
  }

  const args = [
    join(CORE_DIR, "..", "synth", "circuit_apply.py"),
    "--circuit-json", source,
    "--project", project,
    "--plan", plan,
  ];
  if (result) args.push("--result", result);
  if (expectedRevision !== null && expectedRevision !== undefined) {
    args.push("--expected-revision", String(expectedRevision));
  }
  if (commit) args.push("--commit");

  const childEnv = {};
  for (const key of ["PHICAD_KICAD_CLI", "PHICAD_BUILD", "PHICAD_VENV", "PHICAD_SYNTH_PYTHON"]) {
    if (process.env[key]) childEnv[key] = process.env[key];
  }
  let execution;
  try {
    execution = await run(APPLY_PYTHON, args, {
      cwd: CORE_DIR,
      env: minimalEnv(childEnv),
      timeoutMs: APPLY_LIMITS.timeoutMs,
      maxOutputBytes: APPLY_LIMITS.maxJsonBytes,
      signal,
    });
  } catch (err) {
    const reason = err instanceof ExecError ? `${err.code}: ${err.message}` : String(err);
    return unavailable(reason, { project });
  }

  const output = parseChildJson(execution.stdout);
  if (!output || typeof output !== "object") {
    return unavailable(`apply service produced no JSON result (exit ${execution.code})`, { project });
  }
  return {
    ...output,
    service_exit_code: execution.code,
    stderr: (execution.stderr ?? "").slice(-4000),
  };
}
