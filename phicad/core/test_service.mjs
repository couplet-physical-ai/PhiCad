//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Tests for the Platform service operation:
//   phicad-core verify --circuit-json <in> --output-dir <dir> --result <out.json>
//
// The contract this suite defends: the service checks THE DOCUMENT IT WAS GIVEN, writes only where
// it was told, refuses anything stale, and never collapses one tool's opinion into another's.
//
// Run: bun phicad/core/test_service.mjs

import { strict as assert } from "node:assert";
import { existsSync } from "node:fs";
import { mkdtemp, mkdir, readFile, rm, stat, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { LIMITS } from "./exec.mjs";
import {
  CANONICAL_ORIGINS, SERVICE_CONTRACT_VERSION, assertCanonicalOrigin, verifyCircuitJson,
} from "./verify-service.mjs";
import { bunBinary, kicadCliBinary, tsciBinary } from "./toolchain.mjs";

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
    console.log(`  FAIL  ${name}\n        ${err.message}`);
  }
}
function skip(reason) { const e = new Error(reason); e.__skip = true; throw e; }

const HAVE_ENGINE = Boolean(bunBinary() && tsciBinary());
const HAVE_KICAD = Boolean(kicadCliBinary());
const HAVE_ALL = HAVE_ENGINE && HAVE_KICAD;

/** A real Circuit JSON document, produced once by the engine and reused. */
let REAL_CJ = null;
async function realCircuitJson() {
  if (REAL_CJ) return REAL_CJ;
  const { evaluateCircuit } = await import("./index.mjs");
  const r = await evaluateCircuit(join(import.meta.dirname, "examples", "blinky.tsx"),
                                 { timeoutMs: 240_000 });
  if (!r.ok) throw new Error(`fixture build failed: ${r.findings.map((f) => f.message)}`);
  const dir = await mkdtemp(join(tmpdir(), "phicad-svc-fixture-"));
  REAL_CJ = join(dir, "circuit.json");
  await writeFile(REAL_CJ, JSON.stringify(r.circuitJson));
  return REAL_CJ;
}

async function freshDir(tag) {
  return await mkdtemp(join(tmpdir(), `phicad-svc-${tag}-`));
}

// --------------------------------------------------------------- input validation

await test("both arguments are required", async () => {
  const r = await verifyCircuitJson({ circuitJsonPath: "/x.json" });
  assert.equal(r.ok, false);
  assert.equal(r.diagnostics[0].code, "verify.bad_request");
});

await test("a missing input is reported, not guessed at", async () => {
  const out = await freshDir("missing");
  const r = await verifyCircuitJson({
    circuitJsonPath: join(tmpdir(), "phicad-absent-cj-xyz.json"), outputDir: out,
  });
  assert.equal(r.diagnostics[0].code, "verify.source_missing");
  await rm(out, { recursive: true, force: true });
});

await test("unparseable JSON is a diagnostic, not a crash", async () => {
  const dir = await freshDir("badjson");
  const bad = join(dir, "bad.json");
  await writeFile(bad, "{ not json at all");
  const out = await freshDir("badjson-out");
  const r = await verifyCircuitJson({ circuitJsonPath: bad, outputDir: out });
  assert.equal(r.diagnostics[0].code, "verify.circuit_json_unparseable");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

await test("a non-array document is refused", async () => {
  // Circuit JSON is an array of typed elements. An object might be a manifest, a config or a
  // different tool's output; exporting it would mean exporting something the caller never described.
  const dir = await freshDir("obj");
  const f = join(dir, "obj.json");
  await writeFile(f, JSON.stringify({ type: "pcb_board" }));
  const out = await freshDir("obj-out");
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.equal(r.diagnostics[0].code, "verify.circuit_json_invalid");
  assert.match(r.diagnostics[0].message, /must be an array/);
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

await test("an element with no type is refused, and the index is named", async () => {
  const dir = await freshDir("untyped");
  const f = join(dir, "u.json");
  await writeFile(f, JSON.stringify([{ type: "pcb_board" }, { width: 5 }]));
  const out = await freshDir("untyped-out");
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.equal(r.diagnostics[0].code, "verify.circuit_json_invalid");
  assert.match(r.diagnostics[0].message, /element 1/, "the caller must be told WHICH element");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

await test("an empty document is refused", async () => {
  const dir = await freshDir("empty");
  const f = join(dir, "e.json");
  await writeFile(f, "[]");
  const out = await freshDir("empty-out");
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.equal(r.diagnostics[0].code, "verify.circuit_json_empty");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

await test("an oversized document is refused before any tool runs", async () => {
  const dir = await freshDir("big");
  const f = join(dir, "big.json");
  const fh = await (await import("node:fs/promises")).open(f, "w");
  await fh.truncate(LIMITS.maxArtifactBytes + 1024);
  await fh.close();
  const out = await freshDir("big-out");
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.equal(r.diagnostics[0].code, "verify.source_too_large");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

// --------------------------------------------------------------- output directory discipline

await test("a non-fresh output directory is REFUSED, never mixed into", async () => {
  // The requirement this exists for: the Platform will not consume an internally chosen or stale
  // directory, and a leftover file in the result would make the manifest describe two runs.
  const dir = await freshDir("dirty");
  const f = join(dir, "cj.json");
  await writeFile(f, JSON.stringify([{ type: "pcb_board", width: 10, height: 10 }]));
  const out = await freshDir("dirty-out");
  await writeFile(join(out, "leftover.kicad_pcb"), "(kicad_pcb from a previous run)");
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.equal(r.ok, false);
  assert.equal(r.diagnostics[0].code, "verify.output_dir_not_fresh");
  assert.match(r.diagnostics[0].message, /leftover\.kicad_pcb/, "name the offending file");
  assert.equal(r.output_dir, out, "a failure must still say which directory it refers to");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

await test("a dotfile does not make a directory non-fresh", async () => {
  // .gitkeep and friends are how empty directories get created; refusing them would make the
  // service unusable in a checked-out workspace.
  const dir = await freshDir("dot");
  const f = join(dir, "cj.json");
  await writeFile(f, JSON.stringify([{ type: "pcb_board", width: 10, height: 10 }]));
  const out = await freshDir("dot-out");
  await writeFile(join(out, ".gitkeep"), "");
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.notEqual(r.diagnostics[0]?.code, "verify.output_dir_not_fresh");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

await test("the input may not live inside the output directory (and that reason wins)", async () => {
  // Otherwise the input would be reported as one of this run's artifacts. Both this and the
  // freshness check fire here, and the MORE SPECIFIC one must win: telling the caller to clean the
  // directory would send them after the wrong problem when the fault is that they pointed
  // --circuit-json and --output-dir at the same place. Found by this test failing the other way.
  const out = await freshDir("inside");
  const f = join(out, "cj.json");
  await writeFile(f, JSON.stringify([{ type: "pcb_board", width: 10, height: 10 }]));
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.equal(r.ok, false);
  assert.match(r.diagnostics[0].message, /outside --output-dir/);
  await rm(out, { recursive: true, force: true });
});

// --------------------------------------------------------------- toolchain honesty

await test("a missing checker is never reported as a pass", async () => {
  if (HAVE_KICAD) skip("kicad-cli is installed; the missing-checker path needs it absent");
  const dir = await freshDir("nokicad");
  const f = join(dir, "cj.json");
  await writeFile(f, JSON.stringify([{ type: "pcb_board", width: 10, height: 10 }]));
  const out = await freshDir("nokicad-out");
  const r = await verifyCircuitJson({ circuitJsonPath: f, outputDir: out });
  assert.equal(r.ok, false, "no checker means no verdict, which is not a pass");
  assert.equal(r.diagnostics[0].code, "verify.kicad_cli_missing");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

// --------------------------------------------------------------- the real path

await test("SLOW a real document is exported and independently checked", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("real");
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });

  assert.equal(r.contractVersion, SERVICE_CONTRACT_VERSION);
  assert.equal(typeof r.ok, "boolean");
  assert.ok(r.diagnostics.length > 0, "this fixture is known to produce violations");
  assert.ok(r.artifacts.includes("board.kicad_pcb"), "the exported board must be an artifact");
  assert.ok(r.artifacts.includes("drc.json"), "the DRC report must be an artifact");

  // Every required field of the Platform contract.
  for (const key of ["ok", "diagnostics", "diagnostic_counts", "artifacts", "artifact_hashes",
                     "tool_versions", "circuit_json_hash", "source_hash"]) {
    assert.ok(key in r, `the result must carry ${key}`);
  }
  assert.equal(r.source_hash, null, "no TSX is read, so there is no source hash to declare");
  assert.ok(r.circuit_json_hash, "the checked document must be identified by hash");
  assert.equal(r.tool_versions["kicad-cli"], "10.99.0");
  // tool_versions is FLAT scalar version strings (v1 shape); resolved paths live in tool_paths.
  assert.ok(r.tool_versions.tscircuit, "the engine version must be exact");

  await rm(out, { recursive: true, force: true });
});

await test("SLOW artifacts are relative, inside output_dir, and hashed", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("paths");
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });
  for (const rel of r.artifacts) {
    assert.ok(!rel.startsWith("/"), `${rel} must be relative, not absolute`);
    assert.ok(!rel.split("/").includes(".."), `${rel} must not escape the output directory`);
    assert.ok(existsSync(join(out, rel)), `${rel} must exist inside the output directory`);
    assert.match(r.artifact_hashes[rel], /^[0-9a-f]{64}$/, `${rel} must carry a sha256`);
  }
  assert.equal(Object.keys(r.artifact_hashes).length, r.artifacts.length,
               "every artifact needs a hash, and every hash an artifact");
  await rm(out, { recursive: true, force: true });
});

await test("SLOW every artifact was written by THIS run", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("fresh");
  const before = Date.now();
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });
  for (const rel of r.artifacts) {
    const info = await stat(join(out, rel));
    assert.ok(info.mtimeMs >= before - 2000,
              `${rel} predates this run and would be a stale artifact`);
  }
  await rm(out, { recursive: true, force: true });
});

await test("SLOW diagnostic origins are preserved, never merged", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("origins");
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });
  const origins = new Set(r.diagnostics.map((d) => d.origin));
  for (const o of origins) {
    // EXACTLY TWO canonical origins (manifest schema V2). The set is closed, so a retired lane is
    // not merely unemitted -- it is not accepted either.
    assert.ok(CANONICAL_ORIGINS.includes(o), `unexpected origin ${o}`);
  }
  assert.ok(origins.has("kicad"), "this checker's own findings must be labelled kicad");
  assert.ok(r.diagnostic_counts.by_origin.kicad > 0, "counts must be broken down by origin");
  // The verdict is the INDEPENDENT one: tscircuit warnings inside the input must not flip it.
  assert.equal(r.verdict.origin, "kicad");
  const kicadErrors = r.diagnostics.filter((d) => d.origin === "kicad" && d.severity === "error");
  assert.equal(r.ok, kicadErrors.length === 0,
               "ok must follow the kicad errors only, not the combined set");
  await rm(out, { recursive: true, force: true });
});

await test("SLOW nothing is written outside the supplied directory", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("contained");
  // A sibling directory that must stay empty.
  const sibling = await freshDir("sibling");
  await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });
  const { readdir } = await import("node:fs/promises");
  assert.deepEqual(await readdir(sibling), [], "a sibling directory must be untouched");
  // And the fixture directory holding the input must not have gained a board.
  const fixtureDir = cj.slice(0, cj.lastIndexOf("/"));
  const fixtureFiles = await readdir(fixtureDir);
  assert.deepEqual(fixtureFiles, ["circuit.json"],
                   `the input's directory gained files: ${fixtureFiles}`);
  await rm(out, { recursive: true, force: true });
  await rm(sibling, { recursive: true, force: true });
});

await test("SLOW the same document twice yields the same board hash", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  // The service must be a pure function of the document it is given. If two runs of the SAME
  // Circuit JSON produced different boards, no result could be trusted to describe its input.
  const cj = await realCircuitJson();
  const a = await freshDir("rep-a");
  const b = await freshDir("rep-b");
  const r1 = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: a, timeoutMs: 300_000 });
  const r2 = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: b, timeoutMs: 300_000 });
  assert.equal(r1.artifact_hashes["board.kicad_pcb"], r2.artifact_hashes["board.kicad_pcb"],
               "the same Circuit JSON must export to the same board");
  assert.equal(r1.circuit_json_hash, r2.circuit_json_hash);
  assert.equal(r1.ok, r2.ok);
  await rm(a, { recursive: true, force: true });
  await rm(b, { recursive: true, force: true });
});

await test("SLOW a cancelled service run does not report a pass", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("cancel");
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 300);
  const r = await verifyCircuitJson({
    circuitJsonPath: cj, outputDir: out, signal: controller.signal, timeoutMs: 300_000,
  });
  assert.equal(r.ok, false);
  assert.ok(["verify.cancelled", "verify.export_failed"].includes(r.diagnostics[0].code),
            `unexpected code ${r.diagnostics[0].code}`);
  await rm(out, { recursive: true, force: true });
});

await test("SLOW a short timeout is reported as a timeout, not a failed board", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("timeout");
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 250 });
  assert.equal(r.ok, false);
  assert.ok(["verify.timeout", "verify.export_failed"].includes(r.diagnostics[0].code));
  await rm(out, { recursive: true, force: true });
});

// --------------------------------------------------------------- the caller's filename

await test("SLOW an arbitrary input filename works and matches the canonical name byte-for-byte",
           async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  // REGRESSION: the pinned `tsci export` detects Circuit JSON BY BASENAME (only `*.circuit.json`
  // or exactly `circuit.json`), so `--circuit-json /tmp/platform-real-circuit.json` failed with
  // "Element type ... got: array" while the SAME BYTES named circuit.json passed. The service
  // contract must not depend on the caller's choice of filename. Found by the Platform's
  // integration test, not by this suite -- which is why the assertion is on real bytes.
  const canonical = await realCircuitJson();
  const oddDir = await freshDir("oddname");
  const odd = join(oddDir, "not-named-circuit.json");
  await writeFile(odd, await readFile(canonical));

  const outA = await freshDir("odd-out");
  const outB = await freshDir("canon-out");
  const rOdd = await verifyCircuitJson({ circuitJsonPath: odd, outputDir: outA, timeoutMs: 300_000 });
  const rCanon = await verifyCircuitJson({
    circuitJsonPath: canonical, outputDir: outB, timeoutMs: 300_000,
  });

  assert.ok(rOdd.artifacts.includes("board.kicad_pcb"),
            `an oddly named input must still export a board; got ${JSON.stringify(rOdd.diagnostics[0])}`);
  assert.equal(rOdd.artifact_hashes["board.kicad_pcb"],
               rCanon.artifact_hashes["board.kicad_pcb"],
               "the board must be byte-identical regardless of the input filename");
  assert.equal(rOdd.ok, rCanon.ok, "the verdict must not depend on the filename");
  assert.equal(rOdd.circuit_json_hash, rCanon.circuit_json_hash,
               "the declared hash must be of the ORIGINAL caller bytes, not the staging copy");
  await rm(oddDir, { recursive: true, force: true });
  await rm(outA, { recursive: true, force: true });
  await rm(outB, { recursive: true, force: true });
});

await test("SLOW the staging copy never appears as an artifact or survives the run", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const canonical = await realCircuitJson();
  const oddDir = await freshDir("stage-src");
  const odd = join(oddDir, "some-platform-name.json");
  await writeFile(odd, await readFile(canonical));
  const out = await freshDir("stage-out");
  const r = await verifyCircuitJson({ circuitJsonPath: odd, outputDir: out, timeoutMs: 300_000 });
  for (const rel of r.artifacts) {
    assert.ok(!rel.includes(".phicad-input"), `the staging copy leaked into artifacts: ${rel}`);
  }
  assert.ok(!existsSync(join(out, ".phicad-input")),
            "the staging directory must be removed before the run returns");
  await rm(oddDir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

// --------------------------------------------------------------- the v1 result shape

await test("SLOW tool_versions are scalar version strings, paths live in tool_paths", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  // The Platform needs a MACHINE-VERIFIABLE version lock. A filesystem path is not a version: two
  // machines can share a path and hold different binaries.
  const cj = await realCircuitJson();
  const out = await freshDir("versions");
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });
  for (const [name, value] of Object.entries(r.tool_versions)) {
    assert.equal(typeof value, "string", `tool_versions.${name} must be a scalar string`);
    assert.ok(!value.includes("/"), `tool_versions.${name} looks like a path, not a version: ${value}`);
  }
  for (const key of ["tscircuit", "@tscircuit/eval", "circuit-json", "kicad-cli", "bun",
                     "service-contract"]) {
    assert.ok(r.tool_versions[key], `the version lock needs ${key}`);
  }
  assert.ok(r.tool_paths.bun && r.tool_paths.tsci && r.tool_paths["kicad-cli"],
            "resolved binary paths must still be reported, for diagnosis");
  await rm(out, { recursive: true, force: true });
});

await test("SLOW the result document may live in output_dir but never lists itself", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  const cj = await realCircuitJson();
  const out = await freshDir("selfref");
  const { runVerifyService } = await import("./verify-service.mjs");
  const resultPath = join(out, "result.json");
  const r = await runVerifyService({
    circuitJsonPath: cj, outputDir: out, resultPath, timeoutMs: 300_000,
  });
  assert.ok(!r.artifacts.includes("result.json"),
            "the result is a report ABOUT the run, not a product of it");
  assert.deepEqual(Object.keys(r.artifact_hashes).sort(), [...r.artifacts].sort(),
                   "every artifact needs a hash and every hash an artifact");
  assert.ok(existsSync(resultPath), "the result document must still be written");
  // And what was written must equal what was returned.
  const written = JSON.parse(await readFile(resultPath, "utf8"));
  assert.deepEqual(written.artifacts, r.artifacts);
  await rm(out, { recursive: true, force: true });
});

await test("SLOW every diagnostic carries an origin", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  // The Platform derives per-origin severity counts from diagnostics[], so a missing origin would
  // silently drop findings from its rollup.
  const cj = await realCircuitJson();
  const out = await freshDir("everyorigin");
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });
  for (const d of r.diagnostics) {
    assert.ok(CANONICAL_ORIGINS.includes(d.origin),
              `diagnostic ${d.code} has origin ${JSON.stringify(d.origin)}`);
    assert.ok(["error", "warning", "info"].includes(d.severity),
              `diagnostic ${d.code} has severity ${JSON.stringify(d.severity)}`);
  }
  // The flat counts must agree with the list they summarise.
  const recount = { error: 0, warning: 0, info: 0 };
  for (const d of r.diagnostics) recount[d.severity] += 1;
  assert.equal(r.diagnostic_counts.error, recount.error);
  assert.equal(r.diagnostic_counts.warning, recount.warning);
  await rm(out, { recursive: true, force: true });
});

// --------------------------------------------------------------- the origin enum is closed

await test("there are exactly two canonical origins", () => {
  // Manifest schema V2. An origin names WHO produced a finding, and this service can only speak for
  // its own independent check or relay what the engine wrote -- so the set is two, not "two plus
  // whatever used to exist".
  assert.deepEqual([...CANONICAL_ORIGINS].sort(), ["kicad", "tscircuit"]);
  assert.ok(Object.isFrozen(CANONICAL_ORIGINS), "the enum must not be mutable at runtime");
});

await test("a retired origin is REJECTED, not tolerated", () => {
  // The specific regression: `openpcb` was previously ACCEPTED for wire compatibility. Accepting a
  // value is itself a contract -- a consumer parsing that lane has been promised something this
  // service can never justify -- so it must fail closed like any other unknown value.
  for (const retired of ["openpcb", "OpenPCB", "openPCB"]) {
    assert.throws(() => assertCanonicalOrigin(retired),
                  /unknown diagnostic origin/,
                  `${retired} must be rejected, not accepted`);
  }
});

await test("an unknown origin fails closed", () => {
  for (const bogus of ["freerouting", "", null, undefined, "KICAD", "kicad ", 0, {}]) {
    assert.throws(() => assertCanonicalOrigin(bogus), /unknown diagnostic origin/,
                  `${JSON.stringify(bogus)} must be rejected`);
  }
  // And the canonical pair passes, so the guard is not simply refusing everything.
  for (const ok of CANONICAL_ORIGINS) {
    assert.equal(assertCanonicalOrigin(ok), ok);
  }
});

await test("SLOW no diagnostic escapes with a non-canonical origin", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  // End-to-end: whatever the supplied document contains, every origin that leaves this service is
  // one of the two. Origins are ASSIGNED here and never read from caller data, and this pins that.
  const cj = await realCircuitJson();
  const out = await freshDir("closedenum");
  const r = await verifyCircuitJson({ circuitJsonPath: cj, outputDir: out, timeoutMs: 300_000 });
  for (const d of r.diagnostics) {
    assert.ok(CANONICAL_ORIGINS.includes(d.origin),
              `${d.code} escaped with origin ${JSON.stringify(d.origin)}`);
  }
  for (const key of Object.keys(r.diagnostic_counts.by_origin)) {
    assert.ok(CANONICAL_ORIGINS.includes(key), `by_origin has a non-canonical key: ${key}`);
  }
  // A hostile document cannot inject an origin, because origins are not taken from it.
  const blob = JSON.stringify(r.diagnostics).toLowerCase();
  assert.ok(!blob.includes("openpcb"), "no retired lane may appear in the emitted diagnostics");
  await rm(out, { recursive: true, force: true });
});

await test("SLOW a document claiming its own origin cannot inject one", async () => {
  if (!HAVE_ALL) skip("needs both the tscircuit toolchain and kicad-cli");
  // Belt and braces on the parse question: even if a caller puts an `origin` field on an element,
  // the service assigns origins itself and must not echo it.
  const dir = await freshDir("inject-src");
  const src = join(dir, "hostile.json");
  const real = JSON.parse(await readFile(await realCircuitJson(), "utf8"));
  real.push({ type: "pcb_trace_error", message: "injected", origin: "openpcb" });
  await writeFile(src, JSON.stringify(real));
  const out = await freshDir("inject-out");
  const r = await verifyCircuitJson({ circuitJsonPath: src, outputDir: out, timeoutMs: 300_000 });
  for (const d of r.diagnostics) {
    assert.ok(CANONICAL_ORIGINS.includes(d.origin),
              `an element's own origin field leaked through: ${JSON.stringify(d.origin)}`);
  }
  // The injected finding is still REPORTED -- relabelled as tscircuit, since that is who wrote it
  // into the document -- rather than silently dropped.
  assert.ok(r.diagnostics.some((d) => d.message === "injected" && d.origin === "tscircuit"),
            "a diagnostic in the supplied document must be relayed under the tscircuit origin");
  await rm(dir, { recursive: true, force: true });
  await rm(out, { recursive: true, force: true });
});

console.log(`\n${passed} passed, ${failed} failed, ${skipped} skipped`);
process.exit(failed ? 1 : 0);
