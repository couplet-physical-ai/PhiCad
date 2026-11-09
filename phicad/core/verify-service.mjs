//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
// Copyright The KiCad Developers, see AUTHORS.txt for contributors.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The Platform-facing service operation:
//
//   phicad-core verify --circuit-json <in> --output-dir <dir> --result <out.json>
//
// WHY THIS EXISTS SEPARATELY FROM `build`
// --------------------------------------
// `phicad-core build <tsx>` evaluates a TSX source and then checks the result. That is the right
// shape for a human and for an agent iterating on a design, and the wrong shape for the Platform,
// for one specific reason: THE PLATFORM ALREADY HAS THE CIRCUIT JSON. If it handed us a TSX path
// we would re-evaluate it, and a re-evaluation is not guaranteed to reproduce the artifact the
// Platform is holding -- the engine mints random element ids and orders diagnostics by whichever
// supplier lookup finished first (both measured; see manifest.mjs). Checking a board the caller
// never saw, and reporting it as the answer to their question, is precisely the
// confident-wrong-answer failure this fork keeps finding.
//
// So this operation:
//
//   * takes the CIRCUIT JSON THE CALLER SUPPLIES and never looks for a TSX source,
//   * writes ONLY inside the caller's fresh output directory, and refuses a directory that
//     already contains artifacts rather than silently mixing runs,
//   * runs ONLY the independent KiCad export + check path, with no engine re-evaluation,
//   * records the exact tool versions and a hash of the input it was actually given.
//
// Verified equivalence: `tsci export <circuit.json> -f kicad_pcb` and
// `tsci export <source.tsx> -f kicad_pcb` produced BYTE-IDENTICAL boards for the same design
// (sha256 94841b7baf0955f2..., 8273 B). So taking the Circuit JSON path costs no fidelity; it only
// removes the re-evaluation.
//
// THE CALLER'S FILENAME IS NOT A CONTRACT TERM
// ----------------------------------------------
// The pinned `tsci export` decides whether its input is Circuit JSON BY BASENAME: it accepts only
// `*.circuit.json` or a file named exactly `circuit.json` (main.js, `mpe`: `e.endsWith(".circuit
// .json") || n === "circuit.json"`). There is no input-format flag. So `--circuit-json
// /tmp/platform-real-circuit.json` failed with "Element type ... got: array" purely because of its
// NAME, while the identical bytes named circuit.json passed -- a service contract silently
// depending on the caller's choice of filename.
//
// The fix is a bounded private copy at `<output-dir>/.phicad-input/circuit.json`, removed before
// artifacts are enumerated so it can never appear in the result. It stays inside the directory the
// caller supplied, needs no network, and reads no TSX. `circuit_json_hash` is computed from the
// ORIGINAL caller-supplied bytes, not the copy, so the declared hash always identifies what the
// caller sent.
//
// ORIGINS ARE PRESERVED, NOT COLLAPSED
// -----------------------------------
// Diagnostics carry an `origin`, and there are EXACTLY TWO canonical values: `kicad` for what this
// checker independently produces, and `tscircuit` for diagnostics found INSIDE the supplied Circuit
// JSON, passed through so the Platform can tell who said what. Merging origins would destroy the one
// property that makes an independent check worth running.
//
// The set is CLOSED (manifest schema V2). A retired lane -- notably the withdrawn OpenPCB one -- is
// not merely unused here: accepting it would still be a live schema contract, and a consumer that
// parses an origin this service can never justify has been handed a promise nobody keeps. So
// `assertCanonicalOrigin()` fails closed on anything outside the pair, and a test proves it. This
// costs one comparison and removes a whole class of "the schema said it was possible" bug.

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { copyFile, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { basename, isAbsolute, join, relative, resolve, sep } from "node:path";

import { ExecError, LIMITS, minimalEnv, run } from "./exec.mjs";
import { ERROR, Finding, WARNING, findingsFromCircuitJson, summariseCircuit } from "./index.mjs";
import { findingsFromDrcReport } from "./kicad-verify.mjs";
import {
  bunBinary, bunVersion, engineVersions, kicadCliBinary, kicadVersion, tsciBinary,
} from "./toolchain.mjs";

export const SERVICE_CONTRACT_VERSION = 1;

/**
 * The only diagnostic origins this service may emit or accept.
 *
 * Closed on purpose: an origin names WHO produced a finding, and this service can only ever speak
 * for the independent CAD check (`kicad`) or relay what the engine put in the document
 * (`tscircuit`). Anything else -- a retired component, a typo, a future tool -- must fail closed
 * rather than travel to a consumer as though this service had vouched for it.
 */
export const CANONICAL_ORIGINS = Object.freeze(["kicad", "tscircuit"]);

/** Throw unless `origin` is canonical. Called on every diagnostic before it leaves. */
export function assertCanonicalOrigin(origin) {
  if (!CANONICAL_ORIGINS.includes(origin)) {
    throw new Error(
      `unknown diagnostic origin ${JSON.stringify(origin)}; the canonical origins are `
      + `${CANONICAL_ORIGINS.join(", ")}`,
    );
  }
  return origin;
}

/** sha256 of a file, or null when it is not readable. */
async function fileSha256(path) {
  try {
    return createHash("sha256").update(await readFile(path)).digest("hex");
  } catch {
    return null;
  }
}

/** Every file under a directory as a path relative to it, sorted, bounded. */
async function relativeFiles(dir) {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true, recursive: true });
  } catch {
    return [];
  }
  const out = [];
  for (const entry of entries) {
    if (out.length >= LIMITS.maxArtifactFiles) break;
    if (!entry.isFile()) continue;
    const full = join(entry.parentPath ?? entry.path ?? dir, entry.name);
    out.push(relative(dir, full));
  }
  return out.sort();
}

function serviceFailure(code, message, extra = {}) {
  // `output_dir` is included whenever the caller supplied one, even on failure: the Platform needs
  // to know which directory the (empty) result refers to.
  const finding = new Finding({ code, severity: ERROR, message });
  return {
    ok: false,
    contractVersion: SERVICE_CONTRACT_VERSION,
    service: "PhiCad Verify",
    diagnostics: [{ ...finding.toJSON(), origin: "kicad" }],
    diagnostic_counts: { error: 1, warning: 0, info: 0, by_origin: { kicad: 1 } },
    artifacts: [],
    artifact_hashes: {},
    tool_versions: { ...engineVersions(), "service-contract": String(SERVICE_CONTRACT_VERSION) },
    tool_paths: {},
    ...extra,
  };
}

/**
 * Validate, export and independently check a Circuit JSON document.
 *
 * Returns the result object that is also written to `--result`. `ok` is true only when the
 * independent CAD check ran and reported no errors: a check that could not run is never a pass.
 */
export async function verifyCircuitJson({
  circuitJsonPath,
  outputDir,
  resultPath = null,
  signal = null,
  timeoutMs = LIMITS.timeoutMs,
} = {}) {
  const startedAt = Date.now();

  // ---------------------------------------------------------------- input validation
  if (!circuitJsonPath || !outputDir) {
    return serviceFailure("verify.bad_request",
                          "both --circuit-json and --output-dir are required");
  }
  const inputPath = resolve(circuitJsonPath);
  const outDir = resolve(outputDir);

  if (!existsSync(inputPath)) {
    return serviceFailure("verify.source_missing", `no such Circuit JSON: ${inputPath}`);
  }
  const inputStat = await stat(inputPath);
  if (inputStat.size > LIMITS.maxArtifactBytes) {
    return serviceFailure("verify.source_too_large",
                          `Circuit JSON is ${inputStat.size} bytes, over the `
                          + `${LIMITS.maxArtifactBytes} limit`);
  }

  let circuitJson;
  try {
    circuitJson = JSON.parse(await readFile(inputPath, "utf8"));
  } catch (err) {
    return serviceFailure("verify.circuit_json_unparseable",
                          `the Circuit JSON could not be parsed: ${err.message}`);
  }
  // Circuit JSON is an ARRAY of typed elements. Anything else is not Circuit JSON, and guessing
  // would mean exporting something the caller did not describe.
  if (!Array.isArray(circuitJson)) {
    return serviceFailure("verify.circuit_json_invalid",
                          "Circuit JSON must be an array of elements, got "
                          + (circuitJson === null ? "null" : typeof circuitJson));
  }
  if (circuitJson.length === 0) {
    return serviceFailure("verify.circuit_json_empty", "the Circuit JSON contains no elements");
  }
  const untyped = circuitJson.findIndex((e) => !e || typeof e.type !== "string");
  if (untyped !== -1) {
    return serviceFailure("verify.circuit_json_invalid",
                          `element ${untyped} has no string "type"; this is not Circuit JSON`);
  }

  // ---------------------------------------------------------------- output directory
  await mkdir(outDir, { recursive: true });

  // CONTAINMENT IS CHECKED BEFORE FRESHNESS, because the more specific problem must win. When the
  // input sits inside the output directory both checks fire, and reporting "the directory is not
  // fresh" would send the caller off to clean a directory when the real fault is that they pointed
  // the two arguments at the same place. A test pins the ordering.
  const inputRelative = relative(outDir, inputPath);
  if (!inputRelative.startsWith("..") && !isAbsolute(inputRelative)) {
    return serviceFailure("verify.bad_request",
                          "the Circuit JSON must live outside --output-dir, otherwise the input "
                          + "would be reported as one of this run's artifacts",
                          { output_dir: outDir });
  }

  // The directory must be FRESH. A pre-existing artifact is refused rather than mixed in: the
  // caller asked us to describe what THIS run produced, and a stale file in the answer would make
  // the manifest a lie. (`.gitkeep`-style dotfiles are tolerated; real files are not.)
  const preexisting = (await relativeFiles(outDir)).filter((f) => !basename(f).startsWith("."));
  if (preexisting.length > 0) {
    return serviceFailure(
      "verify.output_dir_not_fresh",
      `the output directory already contains ${preexisting.length} file(s) `
      + `(${preexisting.slice(0, 3).join(", ")}${preexisting.length > 3 ? ", ..." : ""}); `
      + "supply a fresh directory so the result describes only this run",
      { artifacts: [], output_dir: outDir },
    );
  }

  // ---------------------------------------------------------------- toolchain
  const bun = bunBinary();
  const tsci = tsciBinary();
  const cli = kicadCliBinary();
  // VERSIONS AND PATHS ARE DIFFERENT THINGS. `tool_versions` is exact scalar version strings only,
  // so the Platform can machine-verify a version lock; a path is not a version, since two machines
  // can share a path and hold different binaries. Paths move to `tool_paths` for diagnosis.
  const tool_versions = {
    ...engineVersions(),
    "kicad-cli": await kicadVersion({ signal }),
    bun: await bunVersion({ signal }),
    "service-contract": String(SERVICE_CONTRACT_VERSION),
  };
  const tool_paths = { bun, tsci, "kicad-cli": cli };
  if (!bun || !tsci) {
    return serviceFailure("verify.engine_missing",
                          "the tscircuit CLI is not installed in phicad/core/node_modules; "
                          + "run `bun install` there (no download is performed at run time)",
                          { tool_versions, tool_paths, output_dir: outDir });
  }
  if (!cli) {
    // The independent checker is the entire point of this service. Without it there is nothing to
    // report, and reporting ok:true would be a lie.
    return serviceFailure("verify.kicad_cli_missing",
                          "kicad-cli was not found, so no independent CAD check could be "
                          + "performed; set PHICAD_KICAD_CLI or PHICAD_BUILD",
                          { tool_versions, tool_paths, output_dir: outDir });
  }

  // ---------------------------------------------------------------- export (no re-evaluation)
  const boardName = "board.kicad_pcb";
  const boardPath = join(outDir, boardName);

  // A private, bounded staging copy under a name the pinned engine will recognise. See the header
  // note: the engine detects Circuit JSON by basename, and the caller's filename is not a contract
  // term. This directory is removed before artifacts are enumerated.
  const stageDir = join(outDir, ".phicad-input");
  const stagedInput = join(stageDir, "circuit.json");
  const cleanupStage = async () => { await rm(stageDir, { recursive: true, force: true }); };
  try {
    await mkdir(stageDir, { recursive: true });
    await copyFile(inputPath, stagedInput);
  } catch (err) {
    await cleanupStage();
    return serviceFailure("verify.stage_failed",
                          `the supplied Circuit JSON could not be staged for export: ${err.message}`,
                          { tool_versions, tool_paths, output_dir: outDir });
  }

  try {
    // The INPUT is the Circuit JSON the caller supplied, byte-for-byte, under a name the engine
    // accepts. No TSX is read, so there is no chance of checking a different revision.
    const exported = await run(bun, [tsci, "export", stagedInput, "-f", "kicad_pcb",
                                     "-o", boardPath], {
      cwd: outDir,
      env: minimalEnv(),
      timeoutMs,
      signal,
    });
    if (!existsSync(boardPath)) {
      await cleanupStage();
      const tail = (exported.stderr || exported.stdout || "").trim().split("\n").slice(-3).join(" / ");
      return serviceFailure("verify.export_failed",
                            tail || `the export exited ${exported.code} without writing a board`,
                            { tool_versions, tool_paths, output_dir: outDir });
    }
  } catch (err) {
    await cleanupStage();
    const code = err instanceof ExecError ? err.code : "exit";
    return serviceFailure(
      code === "timeout" ? "verify.timeout"
        : code === "cancelled" ? "verify.cancelled" : "verify.export_failed",
      err.message,
      { tool_versions, tool_paths, output_dir: outDir },
    );
  }

  // The staging copy has served its purpose. It is removed HERE, before anything enumerates the
  // output directory, so it cannot be reported as one of this run's artifacts.
  await cleanupStage();

  // Freshness: the board must have been written by THIS run.
  const boardStat = await stat(boardPath);
  if (boardStat.mtimeMs < startedAt - 1000) {
    return serviceFailure("verify.stale_artifact_only",
                          "the exported board predates this run and was rejected as stale",
                          { tool_versions, tool_paths, output_dir: outDir });
  }

  // ---------------------------------------------------------------- independent CAD check
  const reportName = "drc.json";
  const reportPath = join(outDir, reportName);
  let drcExec;
  try {
    drcExec = await run(cli, ["pcb", "drc", "--format", "json", "--severity-all",
                              "--refill-zones", "-o", reportPath, boardPath], {
      cwd: outDir,
      env: minimalEnv({ KICAD_RUN_FROM_BUILD_DIR: "1" }),
      timeoutMs,
      signal,
    });
  } catch (err) {
    const code = err instanceof ExecError ? err.code : "exit";
    return serviceFailure(
      code === "timeout" ? "verify.drc_timeout"
        : code === "cancelled" ? "verify.cancelled" : "verify.drc_failed",
      err.message,
      { tool_versions, tool_paths, output_dir: outDir },
    );
  }

  // `kicad-cli pcb drc` EXITS 0 WITH VIOLATIONS, so the report -- not the exit code -- is the only
  // truth. An absent report means the board is unchecked, which is a failure, not a pass.
  if (!existsSync(reportPath)) {
    return serviceFailure("verify.drc_produced_no_report",
                          `kicad-cli exited ${drcExec.code} but wrote no DRC report, so the `
                          + "board is unchecked",
                          { tool_versions, tool_paths, output_dir: outDir });
  }
  const reportStat = await stat(reportPath);
  if (reportStat.mtimeMs < startedAt - 1000) {
    return serviceFailure("verify.stale_drc_report",
                          "the DRC report predates this run and was rejected as stale",
                          { tool_versions, tool_paths, output_dir: outDir });
  }

  let report;
  try {
    report = JSON.parse(await readFile(reportPath, "utf8"));
  } catch (err) {
    return serviceFailure("verify.drc_report_unreadable",
                          `the DRC report could not be parsed: ${err.message}`,
                          { tool_versions, tool_paths, output_dir: outDir });
  }

  // ---------------------------------------------------------------- assemble the result
  // KiCad findings are what THIS checker independently produced.
  const kicadDiagnostics = findingsFromDrcReport(report)
    .map((f) => ({ ...f.toJSON(), origin: "kicad" }));

  // tscircuit diagnostics already present in the supplied document are passed through with their
  // own origin, so the Platform can attribute every message. They are NOT re-derived and NOT
  // merged into the kicad verdict.
  const tscircuitDiagnostics = findingsFromCircuitJson(circuitJson)
    .map((f) => ({ ...f.toJSON(), origin: "tscircuit" }));

  const diagnostics = [...kicadDiagnostics, ...tscircuitDiagnostics];
  const counts = { error: 0, warning: 0, info: 0, by_origin: {} };
  for (const d of diagnostics) {
    // Fail closed rather than emit an origin a consumer cannot trust. This is a guard against a
    // future edit here, not against the caller: the origins above are assigned by this function and
    // never read from the supplied document.
    try {
      assertCanonicalOrigin(d.origin);
    } catch (err) {
      return serviceFailure("verify.invalid_origin", err.message,
                            { tool_versions, tool_paths, output_dir: outDir });
    }
    counts[d.severity] = (counts[d.severity] ?? 0) + 1;
    counts.by_origin[d.origin] = (counts.by_origin[d.origin] ?? 0) + 1;
  }

  // Artifacts are RELATIVE to output_dir, and every one is verified to sit inside it.
  //
  // The result document is allowed to live inside output_dir, but MUST NOT list itself: it is the
  // report about the run, not a product of it, and its own hash cannot be included in a file that
  // does not exist yet. Excluded by path, not by name, so a file that merely happens to be called
  // result.json is still reported.
  const resultRelative = resultPath
    ? relative(outDir, resolve(resultPath))
    : null;
  const selfReported = resultRelative
        && !resultRelative.startsWith("..") && !isAbsolute(resultRelative)
    ? resultRelative
    : null;
  const artifacts = (await relativeFiles(outDir)).filter((rel) => rel !== selfReported);
  const artifact_hashes = {};
  let escaped = null;
  for (const rel of artifacts) {
    const full = join(outDir, rel);
    if (relative(outDir, full).startsWith("..")) { escaped = rel; break; }
    artifact_hashes[rel] = await fileSha256(full);
  }
  if (escaped) {
    return serviceFailure("verify.artifact_escaped_output_dir",
                          `an artifact resolved outside the output directory: ${escaped}`,
                          { tool_versions, tool_paths, output_dir: outDir });
  }

  // `ok` reflects the INDEPENDENT check only. tscircuit warnings carried in the input do not make
  // this service's verdict fail, and tscircuit's own success does not make it pass.
  const ok = !kicadDiagnostics.some((d) => d.severity === ERROR);

  return {
    ok,
    contractVersion: SERVICE_CONTRACT_VERSION,
    service: "PhiCad Verify",
    operation: "verify",
    verdict: {
      origin: "kicad",
      ok,
      note: "independent CAD check against the design rules recorded in the exported board; "
            + "not a fabrication-readiness verdict. Origins are preserved, never merged.",
    },
    circuit: summariseCircuit(circuitJson),
    diagnostics,
    diagnostic_counts: counts,
    artifacts,
    artifact_hashes,
    tool_versions,
    tool_paths,
    // Declared so the Platform can prove the result belongs to the document it sent.
    circuit_json_hash: await fileSha256(inputPath),
    source_hash: null,   // no TSX was read: this service never re-evaluates a source
    output_dir: outDir,
    duration_ms: Date.now() - startedAt,
    drc_exit_code: drcExec.code,
  };
}

/** Run the operation and write the result document. Returns the result. */
export async function runVerifyService(options) {
  // resultPath is passed INTO the check so the result document can be excluded from its own
  // artifact list.
  const result = await verifyCircuitJson(options);
  if (options.resultPath) {
    const target = resolve(options.resultPath);
    await mkdir(resolve(target, ".."), { recursive: true }).catch(() => {});
    await writeFile(target, `${JSON.stringify(result, null, 2)}\n`);
  }
  return result;
}
