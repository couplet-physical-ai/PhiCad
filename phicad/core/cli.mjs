#!/usr/bin/env bun
//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// phicad-core: the thin command line over the engine, so the bash `phicad` front, the MCP
// server, the app and a phi tab all reach the same code instead of three near-copies.
//
// Human output is the default and JSON is a flag, matching the rest of the fork.

import { evaluateCircuit, exportCircuit, summarise, summariseCircuit } from "./index.mjs";
import { verifyWithKicad } from "./kicad-verify.mjs";
import { buildManifest } from "./manifest.mjs";
import { launchRequest, launchTargets, validateLaunchRequest } from "./launch.mjs";
import { SERVICE_CONTRACT_VERSION, runVerifyService } from "./verify-service.mjs";
import { runApply } from "./apply-service.mjs";
import { capabilities, engineVersions } from "./toolchain.mjs";

const USAGE = `PhiCad Verify — build a tscircuit design, check it independently

  phicad-core verify --circuit-json <in> --output-dir <dir> --result <out.json>
                                              the Platform service operation: validate the SUPPLIED
                                              Circuit JSON, export it, and run ONLY the independent
                                              KiCad check. Never re-evaluates a TSX source, never
                                              writes outside <dir>, never reuses a stale artifact.
  phicad-core apply   --circuit-json <in> --project <dir-or-.kicad_pro> --plan <out.json>
                       [--result <out.json>] [--expected-revision <hash>] [--commit]
                                              plan or commit Circuit JSON into native PhiCad
  phicad-core capabilities [--json]           what this machine can do, and tool versions
  phicad-core --version [--json]              exact tool versions and the service contract version
  phicad-core --help                          this message
  phicad-core check    <file.tsx> [--json]    evaluate, report tscircuit diagnostics
  phicad-core build    <file.tsx> [--json]    evaluate, then run KiCad's CAD check
  phicad-core manifest <file.tsx> [--json]    build and report artifacts + digests
  phicad-core open <file.tsx> --view <target> [--json]
                                              emit a manifest-bound launch request for another
                                              view (studio, source, phi, docs). Emits only --
                                              the platform launcher does the opening.
  phicad-core export   <file.tsx> -f <fmt> -o <out>
                                              kicad_pcb, kicad_sch, gerbers, specctra-dsn,
                                              glb, step, assembly-svg, pcb-svg, ...

The KiCad step is an independent CAD check against the rules recorded in the exported board.
It is not a fabrication-readiness verdict, and it is not a manufacturing export -- that is
"kicad-cli pcb export" in the same native PhiCad application.
`;

function parse(argv) {
  const args = {
    _: [], json: false, format: null, output: null, view: null,
    circuitJson: null, outputDir: null, result: null, project: null, plan: null,
    expectedRevision: null, commit: false, timeoutMs: null,
    version: false, help: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--json") args.json = true;
    else if (a === "-f" || a === "--format") args.format = argv[++i];
    else if (a === "-o" || a === "--output") args.output = argv[++i];
    else if (a === "--view") args.view = argv[++i];
    else if (a === "--circuit-json") args.circuitJson = argv[++i];
    else if (a === "--output-dir") args.outputDir = argv[++i];
    else if (a === "--result") args.result = argv[++i];
    else if (a === "--project") args.project = argv[++i];
    else if (a === "--plan") args.plan = argv[++i];
    else if (a === "--expected-revision") args.expectedRevision = argv[++i];
    else if (a === "--commit") args.commit = true;
    else if (a === "--timeout-ms") args.timeoutMs = Number(argv[++i]);
    else if (a === "--version" || a === "-V") args.version = true;
    else if (a === "--help" || a === "-h") args.help = true;
    // An unrecognised long flag is an ERROR, not a positional. `--view studio` was silently
    // swallowed into args._ and the command fell back to its default target, which is exactly the
    // kind of confident wrong answer this repo keeps finding.
    else if (a.startsWith("--")) {
      console.error(`unknown flag ${a}`);
      process.exit(2);
    }
    else args._.push(a);
  }
  return args;
}

const GREEN = "\x1b[32m";
const RED = "\x1b[31m";
const YELLOW = "\x1b[33m";
const DIM = "\x1b[2m";
const OFF = "\x1b[0m";

function printFindings(findings) {
  for (const f of findings) {
    const colour = f.severity === "error" ? RED : f.severity === "warning" ? YELLOW : DIM;
    console.log(`  ${DIM}[${f.id}]${OFF} ${colour}${f.severity}${OFF}  ${f.code}`);
    console.log(`      ${f.message}`);
    if (f.refs.length) console.log(`      ${DIM}refs:${OFF} ${f.refs.join(", ")}`);
    if (f.at) {
      console.log(`      ${DIM}at:${OFF}   (${f.at.x_mm.toFixed(2)}, ${f.at.y_mm.toFixed(2)}) mm`
                  + (f.where ? ` on ${f.where}` : ""));
    }
    if (f.fix) console.log(`      ${DIM}fix:${OFF}  ${f.fix}`);
  }
}

const args = parse(process.argv.slice(2));
const [command, file] = args._;

// `--version` and `--help` are answered BEFORE anything else and never require a positional file.
// Packaging and media probes run these first; failing them makes an installed package look broken
// when it is fine.
if (args.version || command === "version") {
  const versions = { ...engineVersions(), "service-contract": String(SERVICE_CONTRACT_VERSION) };
  if (args.json) {
    console.log(JSON.stringify({
      product: "PhiCad Verify",
      tool_versions: versions,
    }, null, 2));
  } else {
    console.log(`PhiCad Verify (phicad-core), service contract v${SERVICE_CONTRACT_VERSION}`);
    for (const [name, value] of Object.entries(versions)) {
      console.log(`  ${name.padEnd(18)} ${value ?? "unknown"}`);
    }
  }
  process.exit(0);
}

if (args.help) {
  console.log(USAGE);
  process.exit(0);
}

// The service operation takes flags rather than a positional file, so it is dispatched before the
// file check below. Its contract is machine-first: the result document is the deliverable, and the
// exit code is 0 whenever the SERVICE ran, non-zero only when it could not answer -- a board with
// violations is a successful check with ok:false, not a failed invocation.
if (command === "apply") {
  const applied = await runApply({
    circuitJsonPath: args.circuitJson,
    projectPath: args.project,
    planPath: args.plan,
    resultPath: args.result,
    expectedRevision: args.expectedRevision,
    commit: args.commit,
  });
  const payload = args.commit ? (applied.result ?? applied) : (applied.plan ?? applied);
  console.log(JSON.stringify(payload, null, 2));
  process.exit(applied.exit_code ?? (applied.result?.status === "applied" ? 0 : 3));
}

if (command === "verify") {
  const result = await runVerifyService({
    circuitJsonPath: args.circuitJson,
    outputDir: args.outputDir,
    resultPath: args.result,
    timeoutMs: args.timeoutMs ?? undefined,
  });
  if (args.json || !args.result) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    const c = result.diagnostic_counts ?? {};
    console.log(`${result.ok ? GREEN + "PASS" + OFF : RED + "FAIL" + OFF}  kicad check  `
                + `${c.error ?? 0} errors, ${c.warning ?? 0} warnings`);
    console.log(`  artifacts ${(result.artifacts ?? []).length} in ${result.output_dir}`);
    console.log(`  result    ${args.result}`);
    if (result.circuit_json_hash) {
      console.log(`  ${DIM}circuit_json_hash ${result.circuit_json_hash.slice(0, 16)}${OFF}`);
    }
  }
  // Distinguish "the service could not answer" from "the board has violations".
  const serviceBroke = (result.diagnostics ?? []).some(
    (d) => typeof d.code === "string"
           && /^verify\.(bad_request|source_|circuit_json_|output_dir_|engine_missing|kicad_cli_missing|export_failed|timeout|cancelled|drc_|stale_|artifact_)/.test(d.code),
  );
  process.exit(serviceBroke ? 2 : 0);
}

// capabilities takes no file, so it is dispatched before the file check below.
if (command === "capabilities") {
  const caps = await capabilities();
  if (args.json) {
    console.log(JSON.stringify({ ok: true, ...caps }, null, 2));
  } else {
    console.log(`${caps.product} — ${caps.role}`);
    console.log(`  harness   ${caps.harness}`);
    const e = caps.engine;
    console.log(`  engine    ${e.available ? GREEN + "available" + OFF : RED + "missing" + OFF}`
                + `  tscircuit ${e.versions.tscircuit ?? "?"}`);
    console.log(`            ${DIM}${e.cli ?? e.note}${OFF}`);
    const c = caps.checker;
    console.log(`  cad check ${c.available ? GREEN + "available" + OFF : YELLOW + "missing" + OFF}`
                + `  ${c.version ? "kicad-cli " + c.version : ""}`);
    console.log(`            ${DIM}${c.note}${OFF}`);
  }
  process.exit(0);
}

if (!command || command === "help" || !file) {
  console.log(USAGE);
  process.exit(command && command !== "help" ? 1 : 0);
}

if (command === "check") {
  const r = await evaluateCircuit(file);
  if (args.json) {
    console.log(JSON.stringify({
      ok: r.ok,
      product: "PhiCad Verify",
      circuit: summariseCircuit(r.circuitJson),
      findings: r.findings.map((f) => f.toJSON()),
      provenance: r.manifest ?? null,
    }, null, 2));
  } else {
    const c = summariseCircuit(r.circuitJson);
    if (r.circuitJson.length) {
      console.log(`${c.components} components, ${c.pads} pads, ${c.traces} traces`
                  + (c.width_mm ? `, ${c.width_mm} x ${c.height_mm} mm` : ""));
    }
    const verdict = r.ok ? `${GREEN}${summarise(r.findings)}${OFF}`
                         : `${RED}${summarise(r.findings)}${OFF}`;
    console.log(verdict);
    printFindings(r.findings);
  }
  process.exit(r.ok ? 0 : 1);
}

if (command === "build") {
  const evaluated = await evaluateCircuit(file);
  const verified = evaluated.circuitJson.length
    ? await verifyWithKicad(file)
    : { ok: false, skipped: false, board: null, findings: [] };

  // The engine's own verdict and the independent one are reported SEPARATELY and never merged:
  // "tscircuit is happy" and "KiCad DRC is happy" are different claims, and collapsing them is
  // how a tool ends up grading its own homework.
  const all = [...evaluated.findings, ...verified.findings];
  if (args.json) {
    console.log(JSON.stringify({
      ok: evaluated.ok && verified.ok,
      product: "PhiCad Verify",
      engine: {
        ok: evaluated.ok,
        tool: "tscircuit",
        findings: evaluated.findings.map((f) => f.toJSON()),
        provenance: evaluated.manifest ?? null,
      },
      verification: {
        ok: verified.ok, skipped: verified.skipped, board: verified.board,
        tool: "kicad-cli",
        findings: verified.findings.map((f) => f.toJSON()),
        provenance: verified.manifest ?? null,
        note: "independent CAD check against the rules recorded in the exported board; "
              + "not a fabrication-readiness verdict",
      },
      circuit: summariseCircuit(evaluated.circuitJson),
    }, null, 2));
  } else {
    const c = summariseCircuit(evaluated.circuitJson);
    console.log(`${evaluated.ok ? GREEN + "PASS" : RED + "FAIL"}${OFF}  engine        `
                + `${c.components} components, ${c.pads} pads, ${c.traces} traces`
                + (c.width_mm ? `, ${c.width_mm} x ${c.height_mm} mm` : "")
                + `  ${DIM}(${summarise(evaluated.findings)})${OFF}`);
    const mark = verified.skipped ? YELLOW + "SKIP" : verified.ok ? GREEN + "PASS" : RED + "FAIL";
    console.log(`${mark}${OFF}  kicad drc     ${summarise(verified.findings)}`
                + (verified.board ? `  ${DIM}${verified.board}${OFF}` : ""));
    printFindings(all);
  }
  process.exit(evaluated.ok && verified.ok ? 0 : 1);
}

if (command === "manifest") {
  const evaluated = await evaluateCircuit(file);
  const manifest = await buildManifest({
    sourcePath: file,
    outDir: evaluated.manifest?.outDir ?? null,
    tools: { engine: evaluated.manifest?.engine ?? null },
    extra: {
      engineOk: evaluated.ok,
      findings: evaluated.findings.map((f) => f.toJSON()),
    },
  });
  if (args.json) {
    console.log(JSON.stringify({ ok: evaluated.ok, ...manifest }, null, 2));
  } else {
    console.log(`${manifest.product}  manifestId ${manifest.manifestId}`);
    console.log(`  source    ${manifest.source}`);
    console.log(`  ${DIM}sha256    ${manifest.sourceSha256}${OFF}`);
    console.log(`  artifacts ${manifest.artifactCount}`);
    for (const a of manifest.artifacts) {
      console.log(`    ${a.name}  ${DIM}${a.bytes} B  ${(a.contentSha256 ?? a.sha256 ?? "").slice(0, 16)}${OFF}`);
    }
    console.log(`  ${DIM}${manifest.boundary.note}${OFF}`);
  }
  process.exit(evaluated.ok ? 0 : 1);
}

if (command === "open") {
  // Emit a launch request. This deliberately does NOT open anything: Verify does not own the other
  // applications and must not mutate them. The platform launcher consumes this descriptor.
  const evaluated = await evaluateCircuit(file);
  const manifest = await buildManifest({
    sourcePath: file,
    outDir: evaluated.manifest?.outDir ?? null,
    tools: { engine: evaluated.manifest?.engine ?? null },
  });
  const view = args.view ?? "source";
  let request;
  try {
    request = launchRequest({ target: view, manifest });
  } catch (err) {
    console.error(`${RED}${err.message}${OFF}`);
    process.exit(1);
  }
  const targets = launchTargets({ sourcePresent: true });
  const chosen = targets.find((t) => t.id === view);
  if (args.json) {
    console.log(JSON.stringify({ ok: true, request, target: chosen, targets }, null, 2));
  } else {
    console.log(`${chosen.label}`);
    console.log(`  project   ${request.project.id}`);
    console.log(`  revision  ${request.revision.manifestId}  ${DIM}${request.revision.expectedHash?.slice(0, 16) ?? "none"}${OFF}`);
    console.log(`  view      ${request.desiredView.view}  ${DIM}handled by ${request.desiredView.handledBy}${OFF}`);
    console.log(`  ${chosen.resolvable ? GREEN + "resolvable" + OFF : YELLOW + "NOT resolvable here" + OFF}  ${DIM}${chosen.reason}${OFF}`);
    console.log(`  ${DIM}emitted only — PhiCad Verify does not open other applications${OFF}`);
    for (const other of targets.filter((t) => t.id !== view)) {
      console.log(`  ${DIM}also: ${other.label} (${other.resolvable ? "resolvable" : "unavailable"})${OFF}`);
    }
  }
  process.exit(0);
}

if (command === "export") {
  if (!args.format || !args.output) {
    console.error("export needs -f <format> and -o <output>");
    process.exit(1);
  }
  const r = await exportCircuit(file, args.format, args.output);
  console.log(r.ok ? r.path : `export failed: ${r.message}`);
  process.exit(r.ok ? 0 : 1);
}

console.error(`unknown command '${command}'`);
console.log(USAGE);
process.exit(1);
