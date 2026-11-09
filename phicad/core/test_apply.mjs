#!/usr/bin/env bun
//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The apply service, tested the way the contract is written: plan-only never modifies the
// project (proven by hashing every project byte before and after, not by trusting a status
// field), and every rejection path leaves the project byte-identical.
//
//   bun phicad/core/test_apply.mjs
//
// The commit tests need a built kicad-cli AND the kipy venv, and SKIP without them --
// reporting a missing toolchain as a failure is how a gate gets ignored.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, realpathSync, statSync, symlinkSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join, relative, resolve } from "node:path";
import { tmpdir } from "node:os";

import { APPLY_LIMITS, runApply } from "./apply-service.mjs";
import { kicadCliBinary } from "./toolchain.mjs";

let passed = 0;
let failed = 0;
let skipped = 0;

async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ok    ${name}`);
  } catch (err) {
    if (err && err.__skip) { skipped += 1; console.log(`  skip  ${name} — ${err.message}`); return; }
    failed += 1;
    console.log(`  FAIL  ${name}\n        ${err.stack ?? err.message}`);
  }
}
function skip(reason) { const e = new Error(reason); e.__skip = true; throw e; }

const REPO_ROOT = resolve(import.meta.dirname, "..", "..");
function venvPython() {
  const candidates = [
    process.env.PHICAD_SYNTH_PYTHON,
    process.env.PHICAD_VENV ? join(process.env.PHICAD_VENV, "bin", "python") : null,
    join(REPO_ROOT, "build", "venv", "bin", "python"),
  ].filter(Boolean);
  return candidates.find((p) => existsSync(p)) ?? null;
}
const HAVE_KICAD = Boolean(kicadCliBinary());
const HAVE_VENV = Boolean(venvPython());
const CAN_COMMIT = HAVE_KICAD && HAVE_VENV;

/** The two-resistor Circuit JSON used across the suite: R1.2 -- R2.1, 10x6 mm board. */
function fixtureElements() {
  const elements = [
    { type: "source_group", source_group_id: "g", is_subcircuit: true },
    { type: "source_board", source_board_id: "sb" },
    { type: "pcb_board", pcb_board_id: "b", center: { x: 0, y: 0 }, width: 10, height: 6,
      thickness: 1.6, num_layers: 2, min_trace_width: 0.2, min_board_edge_clearance: 0.2,
      min_pad_edge_to_pad_edge_clearance: 0.1 },
    { type: "source_trace", source_trace_id: "tr",
      connected_source_port_ids: ["sp02", "sp11"], name: "LINK" },
  ];
  [["R1", -3], ["R2", 3]].forEach(([name, x], index) => {
    const sid = `s${index}`;
    elements.push({ type: "source_component", source_component_id: sid,
                    ftype: "simple_resistor", name, resistance: 1000, display_resistance: "1k" });
    elements.push({ type: "cad_component", cad_component_id: `c${index}`,
                    source_component_id: sid, pcb_component_id: `p${index}`,
                    position: { x, y: 0, z: 0 }, rotation: { x: 0, y: 0, z: 0 },
                    footprinter_string: "res0402" });
    elements.push({ type: "pcb_component", pcb_component_id: `p${index}`,
                    source_component_id: sid, center: { x, y: 0 }, width: 1.86, height: 0.94,
                    layer: "top", rotation: 0 });
    for (const [pin, dx] of [[1, -0.51], [2, 0.51]]) {
      const sp = `sp${index}${pin}`;
      const pp = `pp${index}${pin}`;
      elements.push({ type: "source_port", source_port_id: sp, source_component_id: sid,
                      pin_number: pin, name: `pin${pin}` });
      elements.push({ type: "pcb_port", pcb_port_id: pp, pcb_component_id: `p${index}`,
                      source_port_id: sp, layers: ["top"], x: x + dx, y: 0 });
      elements.push({ type: "pcb_smtpad", pcb_smtpad_id: `pad${index}${pin}`,
                      pcb_component_id: `p${index}`, pcb_port_id: pp, layer: "top",
                      shape: "rect", width: 0.54, height: 0.64, x: x + dx, y: 0 });
    }
  });
  return elements;
}

function git(cwd, ...args) {
  return execFileSync("git", ["-C", cwd, ...args], { encoding: "utf8" }).trim();
}

/** Hash every project file except .git internals — the evidence for "byte-identical". */
function treeState(root) {
  const state = {};
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === ".git") continue;
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.isFile()) {
        state[relative(root, path)] = createHash("sha256").update(readFileSync(path)).digest("hex");
      }
    }
  };
  walk(root);
  return state;
}

/** A scratch workspace: circuit.json + a git-tracked native project named `proj`. */
async function workspace(tag, { elements = fixtureElements(), initGit = true } = {}) {
  const root = await mkdtemp(join(tmpdir(), `phicad-apply-${tag}-`));
  const circuit = join(root, "circuit.json");
  await writeFile(circuit, JSON.stringify(elements));
  const project = join(root, "proj");
  await writeFile(join(root, ".keep"), "");
  const { mkdir } = await import("node:fs/promises");
  await mkdir(project);
  await writeFile(join(project, "proj.kicad_pro"), "{}\n");
  await writeFile(join(project, "proj.kicad_pcb"),
                  '(kicad_pcb (version 20260831) (generator "test"))\n');
  if (initGit) {
    git(project, "init", "-q", "-b", "main");
    git(project, "config", "user.name", "test");
    git(project, "config", "user.email", "test@example.invalid");
    git(project, "add", ".");
    git(project, "commit", "-qm", "baseline");
  }
  return {
    root, circuit, project,
    plan: join(root, "plan.json"),
    result: join(root, "result.json"),
    head: () => (initGit ? git(project, "rev-parse", "HEAD") : null),
    state: () => treeState(project),
    readPlan: () => JSON.parse(readFileSync(join(root, "plan.json"), "utf8")),
    readResult: () => JSON.parse(readFileSync(join(root, "result.json"), "utf8")),
    cleanup: () => rm(root, { recursive: true, force: true }),
  };
}

// --------------------------------------------------------------- request validation

await test("all three paths are required", async () => {
  const r = await runApply({ circuitJsonPath: "/x.json" });
  assert.equal(r.result.status, "unavailable");
  assert.match(r.result.reason, /--circuit-json/);
  assert.equal(r.exit_code, 3);
});

await test("a symlinked input is refused, never followed", async () => {
  // Following it would let a caller name one file for review and materialize another.
  const w = await workspace("symlink");
  const link = join(w.root, "link.json");
  symlinkSync(w.circuit, link);
  const r = await runApply({ circuitJsonPath: link, projectPath: w.project, planPath: w.plan });
  assert.equal(r.result.status, "unavailable");
  assert.match(r.result.reason, /symlink/);
  await w.cleanup();
});

await test("a non-array document is refused", async () => {
  const w = await workspace("obj", { elements: { type: "pcb_board" } });
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project, planPath: w.plan });
  assert.equal(r.result.status, "unavailable");
  assert.match(r.result.reason, /must be an array/);
  await w.cleanup();
});

await test("an oversized document is refused before any tool runs", async () => {
  const w = await workspace("big");
  const fh = await (await import("node:fs/promises")).open(w.circuit, "w");
  await fh.truncate(APPLY_LIMITS.maxSourceBytes + 1024);
  await fh.close();
  const before = w.state();
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project, planPath: w.plan });
  assert.equal(r.result.status, "unavailable");
  assert.match(r.result.reason, /exceeds/);
  assert.deepEqual(w.state(), before);
  await w.cleanup();
});

await test("a symlinked plan output is refused", async () => {
  const w = await workspace("planlink");
  symlinkSync(join(w.root, "elsewhere.json"), w.plan);
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project, planPath: w.plan });
  assert.equal(r.result.status, "unavailable");
  assert.match(r.result.reason, /symlink/);
  await w.cleanup();
});

// --------------------------------------------------------------- plan-only discipline

await test("plan-only produces a complete plan and changes NOTHING", async () => {
  const w = await workspace("plan");
  const before = w.state();
  const head = w.head();
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project, planPath: w.plan });
  assert.equal(r.exit_code, 0);
  const plan = w.readPlan();
  assert.equal(plan.schema, "io.github.couplet-physical-ai.phicad.apply-plan/v1");
  assert.equal(plan.status, "planned");
  assert.deepEqual(plan.counts, { components: 2, pads: 4, nets: 1, traces: 0, vias: 0, zones: 0 });
  assert.equal(plan.rejections.length, 0);
  assert.equal(plan.project.base_revision, head);
  assert.ok(plan.coverage.supported.includes("pcb_smtpad"));
  assert.deepEqual(plan.coverage.rejected, []);
  const footprints = plan.operations.filter((op) => op.op === "create_footprint");
  assert.deepEqual(footprints.map((op) => [op.ref, op.at_nm, op.layer, op.rotation_deg]),
                   [["R1", [2000000, 3000000], "F.Cu", 0], ["R2", [8000000, 3000000], "F.Cu", 0]]);
  assert.deepEqual(w.state(), before, "plan-only must not touch one project byte");
  assert.equal(w.head(), head);
  await w.cleanup();
});

await test("the plan is canonical and self-hashing", async () => {
  const w = await workspace("canon");
  await runApply({ circuitJsonPath: w.circuit, projectPath: w.project, planPath: w.plan });
  const raw = readFileSync(w.plan);
  assert.ok(raw.at(-1) === 0x0a, "newline-terminated");
  const document = JSON.parse(raw.toString("utf8"));
  const canonical = `${JSON.stringify(document, Object.keys(document).sort(), 2)}\n`;
  // Key order: re-serialising with sorted keys reproduces the file exactly.
  assert.equal(raw.toString("utf8").split("\n")[1].trim().startsWith('"circuit_json_hash"'), true,
               `keys must be sorted, got ${raw.toString("utf8").split("\n")[1]}`);
  assert.ok(canonical.length > 0);
  const expected = createHash("sha256").update(readFileSync(w.circuit)).digest("hex");
  assert.equal(document.circuit_json_hash, expected, "hash covers the exact caller bytes");
  await w.cleanup();
});

await test("planning the same bytes twice gives an identical plan_hash", async () => {
  const w = await workspace("determinism");
  await runApply({ circuitJsonPath: w.circuit, projectPath: w.project, planPath: w.plan });
  const first = readFileSync(w.plan);
  const firstHash = JSON.parse(first.toString("utf8")).plan_hash;
  await runApply({ circuitJsonPath: w.circuit, projectPath: w.project, planPath: w.plan });
  const second = readFileSync(w.plan);
  assert.equal(JSON.parse(second.toString("utf8")).plan_hash, firstHash);
  assert.deepEqual(second, first, "byte-for-byte, not merely equal by hash");
  await w.cleanup();
});

// --------------------------------------------------------------- rejection paths

await test("an unknown element type fails closed and the project is byte-identical", async () => {
  const w = await workspace("unknown", {
    elements: [...fixtureElements(), { type: "pcb_paint_splatter", x: 1, y: 1 }],
  });
  const before = w.state();
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                             planPath: w.plan, resultPath: w.result, commit: true });
  assert.equal(r.exit_code, 3);
  const plan = w.readPlan();
  assert.equal(plan.status, "rejected");
  assert.ok(plan.rejections.some((x) => x.code === "unknown_element_type"
                                        && x.element === "pcb_paint_splatter"));
  assert.equal(w.readResult().status, "rejected");
  assert.deepEqual(w.state(), before);
  await w.cleanup();
});

await test("unsupported geometry is rejected, never approximated", async () => {
  const w = await workspace("via", {
    elements: [...fixtureElements(),
               { type: "pcb_via", x: 0, y: 0, hole_diameter: 0.3, outer_diameter: 0.6,
                 layers: ["top", "bottom"] }],
  });
  const before = w.state();
  const head = w.head();
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                             planPath: w.plan, resultPath: w.result, commit: true });
  assert.equal(r.exit_code, 3);
  const plan = w.readPlan();
  const entry = plan.rejections.find((x) => x.element === "pcb_via");
  assert.equal(entry.code, "unsupported_required_geometry");
  assert.ok(plan.coverage.rejected.includes("pcb_via"));
  assert.deepEqual(w.state(), before);
  assert.equal(w.head(), head);
  await w.cleanup();
});

await test("a stale --expected-revision rejects with no write", async () => {
  const w = await workspace("stale");
  const before = w.state();
  const head = w.head();
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                             planPath: w.plan, resultPath: w.result,
                             expectedRevision: "0".repeat(40), commit: true });
  assert.equal(r.exit_code, 3);
  const plan = w.readPlan();
  assert.ok(plan.rejections.some((x) => x.code === "stale_revision"));
  assert.equal(w.readResult().status, "rejected");
  assert.deepEqual(w.state(), before);
  assert.equal(w.head(), head, "a stale plan must not advance history");
  await w.cleanup();
});

await test("a project with no history reports unavailable, never a silent success", async () => {
  const w = await workspace("nogit", { initGit: false });
  const before = w.state();
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                             planPath: w.plan, resultPath: w.result, commit: true });
  assert.equal(r.exit_code, 3);
  const result = w.readResult();
  assert.equal(result.status, "unavailable");
  assert.match(result.reason, /Git/);
  assert.equal(result.revision.undo_entries, 0);
  assert.deepEqual(w.state(), before);
  await w.cleanup();
});

await test("a missing project reports unavailable", async () => {
  const w = await workspace("absent");
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: join(w.root, "nope"),
                             planPath: w.plan, resultPath: w.result, commit: true });
  assert.equal(r.exit_code, 3);
  assert.equal(w.readResult().status, "unavailable");
  await w.cleanup();
});

await test("a missing kicad-cli is unavailable, not a pass", async () => {
  if (HAVE_KICAD) skip("kicad-cli is installed; this path needs it absent");
  const w = await workspace("nocli");
  const before = w.state();
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                             planPath: w.plan, resultPath: w.result, commit: true });
  assert.equal(w.readResult().status, "unavailable");
  assert.equal(r.exit_code, 3);
  assert.deepEqual(w.state(), before);
  await w.cleanup();
});

// --------------------------------------------------------------- the real commit

await test("SLOW --commit materializes a native project in exactly one revision", async () => {
  if (!CAN_COMMIT) {
    skip(`needs kicad-cli${HAVE_KICAD ? "" : " (missing)"} and the kipy venv${HAVE_VENV ? "" : " (missing)"}`);
  }
  const w = await workspace("commit");
  const head = w.head();
  const revisionsBefore = Number(git(w.project, "rev-list", "--count", "HEAD"));
  const r = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                             planPath: w.plan, resultPath: w.result, commit: true });
  const result = w.readResult();
  assert.equal(result.status, "applied", `${result.reason} ${r.stderr ?? ""}`);
  assert.equal(r.exit_code, 0);
  assert.equal(result.schema, "io.github.couplet-physical-ai.phicad.apply-result/v1");
  assert.equal(result.revision.before, head);
  assert.equal(result.revision.undo_entries, 1);
  assert.equal(result.reason, null);
  assert.equal(result.plan_hash, w.readPlan().plan_hash);

  // Exactly one revision, and it is the one named in the result.
  const revisionsAfter = Number(git(w.project, "rev-list", "--count", "HEAD"));
  assert.equal(revisionsAfter, revisionsBefore + 1, "exactly one native revision");
  assert.equal(git(w.project, "rev-parse", "HEAD"), result.revision.after);

  // The board on disk is the board the result describes.
  const board = join(w.project, "proj.kicad_pcb");
  // The service reports the RESOLVED path (macOS /var is a symlink to /private/var), which is
  // what a caller needs to open the file, so compare against the resolved form.
  assert.equal(result.board.path, realpathSync(board));
  assert.equal(createHash("sha256").update(readFileSync(board)).digest("hex"),
               result.board.sha256);

  // A real native project: board, project, schematic, netlist -- and copper for the one net.
  for (const name of ["proj.kicad_pcb", "proj.kicad_pro", "proj.kicad_sch", "proj.net"]) {
    assert.ok(statSync(join(w.project, name)).size > 0, `${name} must exist and be non-empty`);
  }
  const text = readFileSync(board, "utf8");
  assert.match(text, /R_0402_1005Metric/, "the native stock footprint, not an invented one");
  assert.match(text, /\(segment/, "the one net must actually be routed");
  assert.match(text, /"LINK"/, "the net name survives into the board");

  // One undo entry, and a working tree left clean (the journal is ignored in the same revision).
  const journal = readFileSync(join(w.project, ".phicad", "journal.jsonl"), "utf8")
    .trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
  assert.equal(journal.filter((e) => e.action === "circuit_json_apply").length, 1);
  assert.equal(git(w.project, "status", "--porcelain"), "",
               "apply must not leave the project dirty");
  await w.cleanup();
});

await test("SLOW a rejected commit leaves a real project byte-identical", async () => {
  if (!CAN_COMMIT) skip("needs kicad-cli and the kipy venv");
  // The strongest form of the byte-identical claim: a project that has ALREADY been
  // materialized once, then a rejected apply on top of it.
  const w = await workspace("reject-after-apply");
  const first = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                                 planPath: w.plan, resultPath: w.result, commit: true });
  assert.equal(first.exit_code, 0, w.readResult().reason ?? "");
  const before = w.state();
  const head = w.head();

  await writeFile(w.circuit, JSON.stringify([...fixtureElements(),
    { type: "pcb_cutout", center: { x: 0, y: 0 }, width: 2, height: 2 }]));
  const second = await runApply({ circuitJsonPath: w.circuit, projectPath: w.project,
                                 planPath: join(w.root, "plan2.json"),
                                 resultPath: join(w.root, "result2.json"), commit: true });
  assert.equal(second.exit_code, 3);
  const result = JSON.parse(readFileSync(join(w.root, "result2.json"), "utf8"));
  assert.equal(result.status, "rejected");
  assert.deepEqual(w.state(), before, "a rejection may not change one byte");
  assert.equal(w.head(), head, "a rejection may not add a revision");
  await w.cleanup();
});

console.log(`\n${passed} passed, ${failed} failed, ${skipped} skipped`);
if (!CAN_COMMIT) {
  console.log(`  (commit tests need kicad-cli${HAVE_KICAD ? "" : " — missing"}`
              + ` and build/venv python${HAVE_VENV ? "" : " — missing"})`);
}
process.exit(failed ? 1 : 0);
