//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// PhiCad Verify: evaluate a TSX circuit to Circuit JSON, turn what the engine complains
// about into PhiCad findings, and export to the formats the toolchain supports.
//
// WHY THIS WRAPPER EXISTS AT ALL
// ------------------------------
// tscircuit is the engine and we do not reimplement it. But three things have to be ours:
//
//   1. Findings. tscircuit reports problems as ~55 typed circuit-json elements
//      (source_missing_property_error, source_unnamed_trace_warning, ...). PhiCad already has
//      one findings vocabulary, defined in phicad/synth/findings.py, with a STABLE id so a
//      waiver a human grants stays granted. A second, incompatible vocabulary would defeat
//      the point, so tscircuit's elements are translated into ours here.
//
//   2. Two verdicts kept apart. The evaluator's opinion of its own output is not an independent
//      check, so the KiCad CAD check is reported separately and never merged into it. Neither is
//      a fabrication-readiness verdict; see kicad-verify.mjs.
//
//   3. One entry point for four surfaces. CLI, MCP, the app and a phi tab must all go through
//      the same code or they will disagree with each other.
//
// The id derivation is deliberately identical in spirit to findings.py: sha256 over
// (code, sorted refs, position rounded to 1 um), truncated to 12 hex chars, and NOT over the
// message -- rewording a diagnostic must not invalidate a waiver.

import { createHash } from "node:crypto";
import { copyFile, mkdtemp, readdir, readFile, rm, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve, sep } from "node:path";

import { ExecError, LIMITS, minimalEnv, run } from "./exec.mjs";
import { bunBinary, engineVersions, tsciBinary } from "./toolchain.mjs";

export const ERROR = "error";
export const WARNING = "warning";
export const INFO = "info";

const SEVERITY_RANK = { error: 0, warning: 1, info: 2 };

/** A problem, with enough context to locate and fix it. Mirrors phicad/synth/findings.py. */
export class Finding {
  constructor({ code, severity, message, refs = [], where = "", at = null, fix = "" }) {
    this.code = code;
    this.severity = severity;
    this.message = message;
    this.refs = refs;
    this.where = where;
    this.at = at; // { x_mm, y_mm } | null
    this.fix = fix;
  }

  get id() {
    const pos = this.at ? `${this.at.x_mm.toFixed(3)},${this.at.y_mm.toFixed(3)}` : "";
    const payload = [this.code, [...this.refs].sort().join(";"), this.where, pos].join("|");
    return createHash("sha256").update(payload).digest("hex").slice(0, 12);
  }

  toJSON() {
    const out = { id: this.id, code: this.code, severity: this.severity, message: this.message };
    if (this.refs.length) out.refs = this.refs;
    if (this.where) out.where = this.where;
    if (this.at) out.at = this.at;
    if (this.fix) out.fix = this.fix;
    return out;
  }

  oneLine() {
    let s = `${this.severity}: ${this.message}`;
    if (this.refs.length) s += `  [${this.refs.join(", ")}]`;
    if (this.at) s += `  @ (${this.at.x_mm.toFixed(2)}, ${this.at.y_mm.toFixed(2)}) mm`;
    return s;
  }
}

export function sortFindings(list) {
  return [...list].sort((a, b) => {
    const r = (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9);
    if (r) return r;
    if (a.code !== b.code) return a.code < b.code ? -1 : 1;
    return a.id < b.id ? -1 : 1;
  });
}

export function counts(list) {
  const out = { error: 0, warning: 0, info: 0 };
  for (const f of list) if (f.severity in out) out[f.severity] += 1;
  return out;
}

export function summarise(list) {
  const c = counts(list);
  const parts = [];
  for (const [name, n] of [["error", c.error], ["warning", c.warning], ["info", c.info]]) {
    if (n) parts.push(`${n} ${name}${n === 1 ? "" : "s"}`);
  }
  return parts.length ? parts.join(", ") : "no problems";
}

// --------------------------------------------------------------------------- translation
//
// circuit-json marks a problem element by type: anything ending in `_error` or `_warning`.
// Rather than enumerate all ~55 (a list that would rot on every tscircuit release), the
// severity is read off the suffix and the id fields are collected generically. Specific fixes
// are attached only where the advice is genuinely specific.

const FIXES = {
  source_unnamed_trace_warning: "give the trace a name prop so it can be identified",
  source_no_ground_pin_defined_warning: "mark the chip's ground pin, or connect it to a net",
  source_no_power_pin_defined_warning: "mark the chip's power pin, or connect it to a net",
  source_part_not_found_warning: "set a manufacturer part number, or relax the part search",
  source_missing_property_error: "add the missing property to the component",
  source_i2c_misconfigured_error: "check SDA/SCL are both connected and not swapped",
  schematic_component_overlap_warning: "move one symbol, or set schX/schY explicitly",
  pcb_trace_error: "reroute the trace, or widen the clearance for its net",
  pcb_port_not_matched_error: "check the port name against the footprint's pads",
};

/** Every id-ish field on the element, so a UI can select the thing being complained about. */
function refsOf(element) {
  const refs = [];
  for (const [key, value] of Object.entries(element)) {
    if (!key.endsWith("_id") && !key.endsWith("_ids")) continue;
    if (key === `${element.type}_id`) continue; // the finding's own id is not a ref
    if (Array.isArray(value)) refs.push(...value.filter((v) => typeof v === "string"));
    else if (typeof value === "string") refs.push(value);
  }
  if (typeof element.selector === "string") refs.push(element.selector);
  return refs.slice(0, 6);
}

function positionOf(element) {
  const c = element.center ?? element.position ?? null;
  if (c && typeof c.x === "number" && typeof c.y === "number") {
    return { x_mm: c.x, y_mm: c.y };
  }
  return null;
}

/** circuit-json elements -> PhiCad findings. */
export function findingsFromCircuitJson(circuitJson) {
  const out = [];
  for (const element of circuitJson) {
    const type = element?.type;
    if (typeof type !== "string") continue;
    let severity;
    if (type.endsWith("_error")) severity = ERROR;
    else if (type.endsWith("_warning")) severity = WARNING;
    else continue;

    out.push(new Finding({
      code: `tsc.${type}`,
      severity,
      message: element.message || type.replace(/_/g, " "),
      refs: refsOf(element),
      where: element.subcircuit_id || "",
      at: positionOf(element),
      fix: FIXES[type] || "",
    }));
  }
  return sortFindings(out);
}

/** A quick, honest census of what the engine produced. */
export function summariseCircuit(circuitJson) {
  const byType = new Map();
  for (const e of circuitJson) byType.set(e.type, (byType.get(e.type) ?? 0) + 1);
  const board = circuitJson.find((e) => e.type === "pcb_board");
  return {
    elements: circuitJson.length,
    components: byType.get("source_component") ?? 0,
    pads: byType.get("pcb_smtpad") ?? 0,
    traces: byType.get("pcb_trace") ?? 0,
    vias: byType.get("pcb_via") ?? 0,
    nets: byType.get("source_net") ?? 0,
    width_mm: board?.width ?? null,
    height_mm: board?.height ?? null,
  };
}

// --------------------------------------------------------------------------- evaluation

/**
 * Evaluate a .tsx / .ts circuit file to Circuit JSON.
 *
 * WHY THIS RUNS THE `tsci` CLI RATHER THAN CALLING THE EVALUATOR
 * -------------------------------------------------------------
 * @tscircuit/eval exports `runTscircuitCode`, and calling it directly looks like the obvious
 * choice. It is not: `tsci build` drives the evaluator through a worker pool
 * (cli/build/worker-pool.ts) that supplies a platform config, a parts engine, an fsMap and
 * module resolution. Called bare, the evaluator either cannot resolve @tscircuit/core from its
 * own cache location or reports "Not able to guess root component" because none of that setup
 * happened. Both failures were reproduced before this decision was made.
 *
 * The binary is the LOCKED LOCAL one (node_modules/.bin/tsci, installed by the committed
 * lockfile) executed by an absolute bun path -- never `bunx`, which resolves and may download at
 * run time. A missing binary is an error, not an invitation to fetch something.
 *
 * CIRCUIT JSON IS A BUILD BOUNDARY, NOT AUTHORED SOURCE. The TSX is the source of truth; the
 * Circuit JSON this returns is a versioned interchange artifact of one build with one toolchain,
 * and `manifest.engine` records which. Round-tripping it back into authored source is not
 * supported and is not lossless.
 */
export async function evaluateCircuit(filePath, {
  cwd = null,
  signal = null,
  timeoutMs = LIMITS.timeoutMs,
  outDir = null,
} = {}) {
  const abs = resolve(filePath);
  const bun = bunBinary();
  const tsci = tsciBinary();

  if (!existsSync(abs)) {
    return failure("verify.source_missing", `no such file: ${abs}`, [basename(abs)],
                   "check the path");
  }
  if (!bun || !tsci) {
    // A missing toolchain is neither a pass nor a failure of the design: it is the checker
    // being unable to run, which is reported as exactly that.
    return failure("verify.engine_missing",
                   !bun ? "the bun runtime was not found"
                        : "the tscircuit CLI is not installed in phicad/core/node_modules",
                   [], "run `bun install` in phicad/core, or set PHICAD_BUN");
  }

  const sourceStat = await stat(abs);
  if (sourceStat.size > LIMITS.maxSourceBytes) {
    return failure("verify.source_too_large",
                   `source is ${sourceStat.size} bytes, over the ${LIMITS.maxSourceBytes} limit`,
                   [basename(abs)], "split the design into modules");
  }

  // A FRESH directory per run, and the SOURCE IS COPIED INTO IT.
  //
  // `tsci build <abs-path>` writes its dist/ next to the source file, not into the working
  // directory -- verified here: running with cwd set to an empty temp dir left that dir empty and
  // wrote into phicad/core/dist instead. So isolating the output requires isolating the input.
  //
  // This matters more than tidiness. phicad/core/dist already held an earlier run's
  // circuit.json; without isolation a build that produced nothing would have silently returned
  // the previous board as this run's result, which is the worst failure a checker can have.
  const runDir = outDir ?? await mkdtemp(join(tmpdir(), "phicad-verify-"));
  const staged = join(runDir, basename(abs));
  await copyFile(abs, staged);
  const startedAt = Date.now();
  let exec;
  try {
    exec = await run(bun, [tsci, "build", staged], {
      cwd: runDir,
      env: minimalEnv(),
      timeoutMs,
      signal,
    });
  } catch (err) {
    if (!outDir) await rm(runDir, { recursive: true, force: true }).catch(() => {});
    return execFailure(err, abs);
  }

  // The engine reports "Output dist" but actually writes <runDir>/<name>/circuit.json when the
  // entry is an absolute path, and <runDir>/dist/... in other layouts. Both were observed, so the
  // whole run directory is searched -- which is safe precisely because it is fresh and isolated.
  const candidates = (await listJson(runDir))
    .filter((f) => !f.includes(`${sep}.tscircuit${sep}`));
  let circuitJson = null;
  let artifactPath = null;
  for (const candidate of candidates) {
    // Only accept an artifact written DURING this run. An mtime at or before the start means it
    // predates us and is therefore stale, whatever directory it is sitting in.
    const info = await stat(candidate).catch(() => null);
    if (!info || info.mtimeMs < startedAt - 1000) continue;
    if (info.size > LIMITS.maxArtifactBytes) {
      return failure("verify.artifact_too_large",
                     `${basename(candidate)} is ${info.size} bytes, over the `
                     + `${LIMITS.maxArtifactBytes} limit`,
                     [basename(candidate)], "reduce the design size");
    }
    try {
      const parsed = JSON.parse(await readFile(candidate, "utf8"));
      if (Array.isArray(parsed)) { circuitJson = parsed; artifactPath = candidate; break; }
    } catch { /* not the artifact we are looking for */ }
  }

  if (!circuitJson) {
    const stale = candidates.length > 0;
    const tail = (exec.stderr || exec.stdout || "").trim().split("\n").slice(-3).join(" / ");
    return failure(
      stale ? "verify.stale_artifact_only" : "verify.evaluation_failed",
      stale
        ? "the only Circuit JSON present predates this run and was rejected as stale"
        : (tail || `the engine exited ${exec.code} without producing Circuit JSON`),
      [basename(abs)],
      stale ? "re-run in a clean output directory"
            : "fix the circuit source; the message above is from the tscircuit evaluator",
      { durationMs: exec.durationMs, exitCode: exec.code, outDir: runDir },
    );
  }

  const findings = findingsFromCircuitJson(circuitJson);
  return {
    ok: isClean(findings),
    circuitJson,
    findings,
    manifest: {
      source: abs,
      sourceBytes: sourceStat.size,
      artifact: artifactPath,
      outDir: runDir,
      elements: circuitJson.length,
      durationMs: exec.durationMs,
      exitCode: exec.code,
      engine: engineVersions(),
      tool: "tscircuit",
    },
  };
}

/**
 * Export a circuit to a format the engine supports.
 *
 * These are ENGINE exports, for handing the same design to this fork's CAD check and to other
 * tools. Manufacturing export belongs to the native PhiCad application -- the same application that
 * provides Verify -- via `kicad-cli pcb export` (gerbers, drill, ODB++, IPC-2581, position files).
 * Nothing here produces a fabrication package.
 */
export async function exportCircuit(filePath, format, outPath, {
  signal = null,
  timeoutMs = LIMITS.timeoutMs,
} = {}) {
  const abs = resolve(filePath);
  const target = resolve(outPath);
  const bun = bunBinary();
  const tsci = tsciBinary();
  if (!bun || !tsci) {
    return { ok: false, path: null, message: "the tscircuit toolchain is not installed" };
  }
  // Refuse to report a pre-existing file as this run's output.
  const before = existsSync(target) ? (await stat(target)).mtimeMs : null;
  const startedAt = Date.now();
  try {
    const exec = await run(bun, [tsci, "export", abs, "-f", format, "-o", target], {
      cwd: dirname(abs),
      env: minimalEnv(),
      timeoutMs,
      signal,
    });
    if (!existsSync(target)) {
      return {
        ok: false, path: null,
        message: (exec.stderr || exec.stdout || "").trim().split("\n").slice(-2).join(" / ")
                 || `export exited ${exec.code} without writing a file`,
      };
    }
    const info = await stat(target);
    if (before !== null && info.mtimeMs <= startedAt - 1000) {
      return { ok: false, path: null, message: "export produced no new file; refusing a stale one" };
    }
    return { ok: true, path: target, bytes: info.size, durationMs: exec.durationMs, message: "" };
  } catch (err) {
    return {
      ok: false, path: null,
      message: err instanceof ExecError ? `${err.code}: ${err.message}` : String(err),
    };
  }
}

function failure(code, message, refs, fix, manifest = {}) {
  return {
    ok: false,
    circuitJson: [],
    findings: [new Finding({ code, severity: ERROR, message, refs, fix })],
    manifest: { engine: engineVersions(), tool: "tscircuit", ...manifest },
  };
}

/** Turn an ExecError into a finding whose code says which limit or signal ended the run. */
function execFailure(err, sourcePath) {
  const code = err instanceof ExecError ? err.code : "exit";
  const map = {
    timeout: ["verify.timeout", "raise the timeout, or simplify the design"],
    cancelled: ["verify.cancelled", "the caller cancelled this run"],
    output_limit: ["verify.output_limit", "the engine produced more output than the limit allows"],
    spawn: ["verify.engine_missing", "check the toolchain with `phicad core capabilities`"],
  };
  const [findingCode, fix] = map[code] ?? ["verify.evaluation_failed", "inspect the engine output"];
  return {
    ok: false,
    circuitJson: [],
    findings: [new Finding({
      code: findingCode, severity: ERROR,
      message: err.message, refs: [basename(sourcePath)], fix,
    })],
    manifest: {
      engine: engineVersions(), tool: "tscircuit",
      durationMs: err.durationMs ?? 0, failure: code,
    },
  };
}

/** Every .json under a directory, bounded so a runaway build cannot make this walk forever. */
async function listJson(dir) {
  if (!existsSync(dir)) return [];
  const found = [];
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true, recursive: true });
  } catch {
    return [];
  }
  for (const entry of entries) {
    if (found.length >= LIMITS.maxArtifactFiles) break;
    if (entry.isFile() && entry.name.endsWith(".json")) {
      found.push(join(entry.parentPath ?? entry.path ?? dir, entry.name));
    }
  }
  return found;
}

/** True when nothing at error severity was found. Warnings do not fail a check. */
export function isClean(findings) {
  return !findings.some((f) => f.severity === ERROR);
}
