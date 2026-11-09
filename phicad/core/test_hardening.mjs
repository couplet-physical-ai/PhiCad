//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Hardening tests: the failure modes that separate a proof of concept from something that can be
// pointed at a real design.
//
// Each test names the specific bad outcome it prevents. Tests that need the real engine are
// marked SLOW and skipped when the toolchain is absent -- and a skip is reported as a skip, never
// folded into the pass count.
//
// Run: bun phicad/core/test_hardening.mjs

import { strict as assert } from "node:assert";
import { mkdtemp, writeFile, mkdir, utimes, rm } from "node:fs/promises";
import { existsSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { APPLY_LIMITS, runApply } from "./apply-service.mjs";
import { ExecError, LIMITS, minimalEnv, run } from "./exec.mjs";
import { evaluateCircuit } from "./index.mjs";
import { buildManifest } from "./manifest.mjs";
import {
  LAUNCH_CONTRACT_VERSION, LaunchError, launchRequest, launchTargets, projectId,
  validateLaunchRequest,
} from "./launch.mjs";
import { bunBinary, capabilities, kicadCliBinary, tsciBinary } from "./toolchain.mjs";

let passed = 0;
let failed = 0;
let skipped = 0;

async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ok    ${name}`);
  } catch (err) {
    if (err && err.__skip) {
      skipped += 1;
      console.log(`  skip  ${name} — ${err.message}`);
      return;
    }
    failed += 1;
    console.log(`  FAIL  ${name}\n        ${err.message}`);
  }
}

function skip(reason) {
  const e = new Error(reason);
  e.__skip = true;
  throw e;
}

const HAVE_ENGINE = Boolean(bunBinary() && tsciBinary());
const EXAMPLE = join(import.meta.dirname, "examples", "blinky.tsx");

// --------------------------------------------------------------- environment isolation

await test("the child environment carries no secrets", () => {
  process.env.PHICAD_TEST_FAKE_TOKEN = "super-secret";
  process.env.OPENAI_API_KEY = "sk-should-not-propagate";
  process.env.HTTPS_PROXY = "http://gateway.invalid:8080";
  try {
    const env = minimalEnv();
    assert.equal(env.PHICAD_TEST_FAKE_TOKEN, undefined,
                 "an allowlist must not forward an unknown variable");
    assert.equal(env.OPENAI_API_KEY, undefined, "model credentials must never reach a build tool");
    assert.equal(env.HTTPS_PROXY, undefined,
                 "the LLM proxy points at the model gateway and must not be inherited");
    assert.ok(env.PATH, "PATH is the one documented inheritance");
  } finally {
    delete process.env.PHICAD_TEST_FAKE_TOKEN;
    delete process.env.OPENAI_API_KEY;
    delete process.env.HTTPS_PROXY;
  }
});

await test("the allowlist is an allowlist, not a denylist", () => {
  // A denylist of secret-shaped names is always incomplete. This asserts the shape of the
  // mechanism, so a future edit that "adds one more variable to delete" fails here.
  process.env.SOME_FUTURE_CREDENTIAL_NOBODY_LISTED = "x";
  try {
    assert.equal(minimalEnv().SOME_FUTURE_CREDENTIAL_NOBODY_LISTED, undefined);
  } finally {
    delete process.env.SOME_FUTURE_CREDENTIAL_NOBODY_LISTED;
  }
});

// --------------------------------------------------------------- exec: limits and control

await test("timeout terminates the child and reports a timeout", async () => {
  const started = Date.now();
  await assert.rejects(
    () => run("/bin/sh", ["-c", "sleep 30"], { cwd: tmpdir(), timeoutMs: 700 }),
    (err) => {
      assert.ok(err instanceof ExecError);
      assert.equal(err.code, "timeout", "the failure must be identifiable as a timeout");
      return true;
    },
  );
  assert.ok(Date.now() - started < 10_000, "it must not wait for the child's natural exit");
});

await test("cancellation via AbortSignal stops a running child", async () => {
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 300);
  const started = Date.now();
  await assert.rejects(
    () => run("/bin/sh", ["-c", "sleep 30"], {
      cwd: tmpdir(), timeoutMs: 60_000, signal: controller.signal,
    }),
    (err) => {
      assert.equal(err.code, "cancelled", "a cancelled run is distinguishable from a timeout");
      return true;
    },
  );
  assert.ok(Date.now() - started < 10_000, "cancellation must be prompt");
});

await test("a signal already aborted is refused before spawning", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    () => run("/bin/echo", ["hi"], { cwd: tmpdir(), signal: controller.signal }),
    (err) => err.code === "cancelled",
  );
});

await test("oversized output is refused rather than silently truncated", async () => {
  await assert.rejects(
    () => run("/bin/sh", ["-c", "yes abcdefghijklmnop"], {
      cwd: tmpdir(), timeoutMs: 20_000, maxOutputBytes: 64 * 1024,
    }),
    (err) => {
      assert.equal(err.code, "output_limit",
                   "a truncated stream must be an error, because truncated JSON parses as garbage");
      return true;
    },
  );
});

await test("a missing binary is a spawn failure, not a download", async () => {
  await assert.rejects(
    () => run("/nonexistent/phicad-verify-does-not-exist", [], { cwd: tmpdir() }),
    (err) => {
      assert.equal(err.code, "spawn");
      return true;
    },
  );
});

await test("a non-zero exit is returned, not thrown", async () => {
  // `kicad-cli pcb drc` exits 0 with violations, and other tools exit non-zero while still
  // writing a usable report. Conflating "exited non-zero" with "produced nothing" is exactly how
  // a verdict gets read off the wrong signal.
  const r = await run("/bin/sh", ["-c", "echo out; exit 3"], { cwd: tmpdir() });
  assert.equal(r.code, 3);
  assert.match(r.stdout, /out/);
});

// --------------------------------------------------------------- source and toolchain guards

await test("a missing source file is reported as such", async () => {
  const r = await evaluateCircuit(join(tmpdir(), "phicad-verify-absent-xyz.tsx"));
  assert.equal(r.ok, false);
  assert.equal(r.findings[0].code, "verify.source_missing");
});

await test("an oversized source is refused before the engine runs", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phicad-verify-big-"));
  const big = join(dir, "big.tsx");
  await writeFile(big, "x".repeat(LIMITS.maxSourceBytes + 1024));
  try {
    const r = await evaluateCircuit(big);
    assert.equal(r.findings[0].code, "verify.source_too_large");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

await test("capabilities reports facts and never throws", async () => {
  const caps = await capabilities();
  assert.equal(caps.product, "PhiCad Verify");
  assert.equal(typeof caps.engine.available, "boolean");
  assert.equal(typeof caps.checker.available, "boolean");
  assert.match(caps.harness, /Phi/, "the harness must be named, and it is phi");
  assert.match(caps.checker.note, /not a fabrication-readiness verdict|kicad-cli not found/,
               "the CAD check must not be described as a fabrication verdict");
});

// --------------------------------------------------------------- manifest determinism

await test("manifest identity ignores paths and timestamps", async () => {
  const a = await mkdtemp(join(tmpdir(), "phicad-man-a-"));
  const b = await mkdtemp(join(tmpdir(), "phicad-man-b-"));
  const payload = JSON.stringify([{ type: "pcb_smtpad", x: 1, y: 2 }]);
  await mkdir(join(a, "out"), { recursive: true });
  await mkdir(join(b, "out"), { recursive: true });
  await writeFile(join(a, "out", "circuit.json"), payload);
  await writeFile(join(b, "out", "circuit.json"), payload);
  // Different mtimes, different directories, identical content.
  const old = new Date(Date.now() - 86_400_000);
  await utimes(join(b, "out", "circuit.json"), old, old);
  try {
    const m1 = await buildManifest({ outDir: a });
    const m2 = await buildManifest({ outDir: b });
    assert.equal(m1.manifestId, m2.manifestId,
                 "identity must depend on content and tools, not on where or when");
  } finally {
    await rm(a, { recursive: true, force: true });
    await rm(b, { recursive: true, force: true });
  }
});

await test("manifest identity changes when content changes", async () => {
  const a = await mkdtemp(join(tmpdir(), "phicad-man-c-"));
  const b = await mkdtemp(join(tmpdir(), "phicad-man-d-"));
  await writeFile(join(a, "circuit.json"), JSON.stringify([{ type: "pcb_smtpad", x: 1 }]));
  await writeFile(join(b, "circuit.json"), JSON.stringify([{ type: "pcb_smtpad", x: 999 }]));
  try {
    const m1 = await buildManifest({ outDir: a });
    const m2 = await buildManifest({ outDir: b });
    assert.notEqual(m1.manifestId, m2.manifestId, "a moved pad is a different result");
  } finally {
    await rm(a, { recursive: true, force: true });
    await rm(b, { recursive: true, force: true });
  }
});

await test("manifest identity ignores the engine's random element ids", async () => {
  // tscircuit mints a nanoid per diagnostic element, so two builds of one unchanged source differ
  // byte-for-byte. Verified on 2026-09-15: the only diff between two runs was four
  // `*_warning_id` values. Without this, every run would look like a new result.
  const a = await mkdtemp(join(tmpdir(), "phicad-man-e-"));
  const b = await mkdtemp(join(tmpdir(), "phicad-man-f-"));
  await writeFile(join(a, "circuit.json"), JSON.stringify(
    [{ type: "source_unnamed_trace_warning", source_unnamed_trace_warning_id: "w_AAAA",
       message: "unnamed" }]));
  await writeFile(join(b, "circuit.json"), JSON.stringify(
    [{ type: "source_unnamed_trace_warning", source_unnamed_trace_warning_id: "w_ZZZZ",
       message: "unnamed" }]));
  try {
    const m1 = await buildManifest({ outDir: a });
    const m2 = await buildManifest({ outDir: b });
    assert.equal(m1.manifestId, m2.manifestId,
                 "a regenerated id is not a design change");
    assert.notEqual(m1.artifacts[0].sha256, m2.artifacts[0].sha256,
                    "the raw digest still records that the bytes differ");
  } finally {
    await rm(a, { recursive: true, force: true });
    await rm(b, { recursive: true, force: true });
  }
});

await test("manifest identity ignores diagnostic ORDER, not just ids", async () => {
  // The engine resolves parts concurrently, so the array order of diagnostics follows whichever
  // supplier lookup finished first: element 52 was R1/C11702 in one run and D1/C965799 in the next.
  // Stripping ids alone left the manifest unstable, and the first version of this suite missed it
  // by using single-element fixtures. This is that gap, closed.
  const a = await mkdtemp(join(tmpdir(), "phicad-man-o1-"));
  const b = await mkdtemp(join(tmpdir(), "phicad-man-o2-"));
  const one = { type: "supplier_footprint_mismatch_warning", w_id: "x1", part: "C11702" };
  const two = { type: "supplier_footprint_mismatch_warning", w_id: "x2", part: "C965799" };
  await writeFile(join(a, "circuit.json"), JSON.stringify([one, two]));
  await writeFile(join(b, "circuit.json"), JSON.stringify([two, one]));
  try {
    const m1 = await buildManifest({ outDir: a });
    const m2 = await buildManifest({ outDir: b });
    assert.equal(m1.manifestId, m2.manifestId,
                 "concurrency-dependent ordering is not a design change");
  } finally {
    await rm(a, { recursive: true, force: true });
    await rm(b, { recursive: true, force: true });
  }
});

await test("manifest identity still notices a dropped element", async () => {
  // Sorting must not become "ignore the contents": a missing diagnostic is a real difference.
  const a = await mkdtemp(join(tmpdir(), "phicad-man-p1-"));
  const b = await mkdtemp(join(tmpdir(), "phicad-man-p2-"));
  const one = { type: "warn_a", w_id: "x1" };
  const two = { type: "warn_b", w_id: "x2" };
  await writeFile(join(a, "circuit.json"), JSON.stringify([one, two]));
  await writeFile(join(b, "circuit.json"), JSON.stringify([one]));
  try {
    assert.notEqual(
      (await buildManifest({ outDir: a })).manifestId,
      (await buildManifest({ outDir: b })).manifestId,
      "losing a diagnostic must change identity",
    );
  } finally {
    await rm(a, { recursive: true, force: true });
    await rm(b, { recursive: true, force: true });
  }
});

await test("manifest records the build/interchange boundary explicitly", async () => {
  const m = await buildManifest({ outDir: null });
  assert.equal(m.boundary.lossless, false,
               "Circuit JSON is a build artifact, not authored source");
  assert.match(m.boundary.note, /not authored source/);
});

await test("an oversized artifact is recorded without a digest, not omitted", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phicad-man-big-"));
  const f = join(dir, "huge.bin");
  // Sparse-ish write: allocate past the artifact limit without holding it in memory.
  const fh = await import("node:fs/promises").then((m) => m.open(f, "w"));
  await fh.truncate(LIMITS.maxArtifactBytes + 1024);
  await fh.close();
  try {
    const m = await buildManifest({ outDir: dir });
    assert.equal(m.artifacts.length, 1, "an oversized file must still be visible");
    assert.equal(m.artifacts[0].oversized, true);
    assert.equal(m.artifacts[0].sha256, null, "we do not hash past the limit");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// --------------------------------------------------------------- stale artifacts

await test("a stale artifact in the output directory is rejected", async () => {
  if (!HAVE_ENGINE) skip("tscircuit toolchain not installed");
  const dir = await mkdtemp(join(tmpdir(), "phicad-stale-"));
  // Plant a plausible but old result, then run a design that cannot build. The run must report a
  // failure rather than returning the planted board as this run's output.
  await mkdir(join(dir, "broken"), { recursive: true });
  writeFileSync(join(dir, "broken", "circuit.json"),
                JSON.stringify([{ type: "pcb_board", width: 999, height: 999 }]));
  const old = new Date(Date.now() - 86_400_000);
  await utimes(join(dir, "broken", "circuit.json"), old, old);

  const src = join(dir, "broken.tsx");
  await writeFile(src, "export default () => { throw new Error('deliberate') }\n");
  try {
    const r = await evaluateCircuit(src, { outDir: dir, timeoutMs: 120_000 });
    assert.equal(r.ok, false, "a failed build must not report a planted artifact as success");
    assert.equal(r.circuitJson.length, 0, "no stale elements may leak into the result");
    assert.ok(
      ["verify.stale_artifact_only", "verify.evaluation_failed"].includes(r.findings[0].code),
      `unexpected code ${r.findings[0].code}`,
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// --------------------------------------------------------------- real engine paths (SLOW)

await test("SLOW a valid design builds, with provenance", async () => {
  if (!HAVE_ENGINE) skip("tscircuit toolchain not installed");
  const r = await evaluateCircuit(EXAMPLE, { timeoutMs: 240_000 });
  assert.equal(r.ok, true, `expected a clean build, got: ${r.findings.map((f) => f.message)}`);
  assert.ok(r.circuitJson.length > 10, "a real board has elements");
  assert.ok(r.manifest.engine.tscircuit, "the engine version must be recorded");
  assert.ok(r.manifest.artifact, "the artifact path must be recorded");
  assert.ok(existsSync(r.manifest.artifact));
});

await test("SLOW a syntax error is a finding, not a crash", async () => {
  if (!HAVE_ENGINE) skip("tscircuit toolchain not installed");
  const dir = await mkdtemp(join(tmpdir(), "phicad-syntax-"));
  const src = join(dir, "bad.tsx");
  await writeFile(src, "export default () => (<board width=)\n");
  try {
    const r = await evaluateCircuit(src, { timeoutMs: 240_000 });
    assert.equal(r.ok, false);
    assert.equal(r.findings[0].severity, "error");
    assert.ok(r.findings[0].message.length > 0, "the engine's message must be surfaced");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

await test("SLOW a runtime throw is a finding, not a crash", async () => {
  if (!HAVE_ENGINE) skip("tscircuit toolchain not installed");
  const dir = await mkdtemp(join(tmpdir(), "phicad-throw-"));
  const src = join(dir, "boom.tsx");
  await writeFile(src, "export default () => { throw new Error('deliberate failure') }\n");
  try {
    const r = await evaluateCircuit(src, { timeoutMs: 240_000 });
    assert.equal(r.ok, false);
    assert.equal(r.circuitJson.length, 0);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

await test("SLOW an engine build can be cancelled mid-flight", async () => {
  if (!HAVE_ENGINE) skip("tscircuit toolchain not installed");
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 400);
  const r = await evaluateCircuit(EXAMPLE, { signal: controller.signal, timeoutMs: 240_000 });
  assert.equal(r.ok, false, "a cancelled build is not a successful one");
  assert.equal(r.findings[0].code, "verify.cancelled");
});

await test("SLOW an engine build honours a short timeout", async () => {
  if (!HAVE_ENGINE) skip("tscircuit toolchain not installed");
  const r = await evaluateCircuit(EXAMPLE, { timeoutMs: 250 });
  assert.equal(r.ok, false);
  assert.equal(r.findings[0].code, "verify.timeout");
});

await test("SLOW two real builds of one design agree on manifestId", async () => {
  if (!HAVE_ENGINE) skip("tscircuit toolchain not installed");
  // The claim is about the REAL engine, so the test drives the real engine twice. Synthetic
  // fixtures cannot see concurrency-dependent ordering, which is exactly what broke here.
  const first = await evaluateCircuit(EXAMPLE, { timeoutMs: 240_000 });
  const second = await evaluateCircuit(EXAMPLE, { timeoutMs: 240_000 });
  assert.equal(first.ok && second.ok, true, "both builds must succeed for this to mean anything");
  const m1 = await buildManifest({ sourcePath: EXAMPLE, outDir: first.manifest.outDir });
  const m2 = await buildManifest({ sourcePath: EXAMPLE, outDir: second.manifest.outDir });
  assert.equal(m1.manifestId, m2.manifestId,
               `two builds of an unchanged design disagreed: ${m1.manifestId} vs ${m2.manifestId}`);
});

// --------------------------------------------------------------- cross-view launch contract

const FAKE_MANIFEST = {
  manifestId: "aaaabbbbccccdddd",
  source: "/projects/demo/demo.tsx",
  sourceSha256: "1".repeat(64),
  tools: { engine: { tscircuit: "0.0.2561" } },
  artifacts: [{ name: "demo/circuit.json", sha256: "2".repeat(64), contentSha256: "3".repeat(64) }],
};

await test("a launch request carries identity and revision, never credentials", () => {
  const req = launchRequest({ target: "studio", manifest: FAKE_MANIFEST });
  assert.equal(req.contract, "io.github.couplet-physical-ai.phicad.launch");
  assert.equal(req.contractVersion, LAUNCH_CONTRACT_VERSION);
  assert.equal(req.carriesCredentials, false);
  assert.equal(req.revision.manifestId, FAKE_MANIFEST.manifestId);
  assert.equal(req.revision.expectedHash, "3".repeat(64), "the stable content digest is used");
  assert.equal(req.desiredView.target, "studio");
  // The whole descriptor must not contain anything secret-shaped. Asserted over the serialised
  // form so a nested field cannot smuggle one in.
  const blob = JSON.stringify(req).toLowerCase();
  for (const forbidden of ["token", "secret", "api_key", "apikey", "password", "bearer"]) {
    assert.ok(!blob.includes(forbidden), `a launch request must not contain "${forbidden}"`);
  }
});

await test("project identity is stable across revisions", () => {
  // A project must not become a different project when someone edits it, so identity comes from
  // the source, not from the manifest id.
  const a = launchRequest({ target: "source", manifest: FAKE_MANIFEST });
  const b = launchRequest({
    target: "source",
    manifest: { ...FAKE_MANIFEST, manifestId: "9999888877776666" },
  });
  assert.equal(a.project.id, b.project.id);
  assert.notEqual(a.revision.manifestId, b.revision.manifestId);
  assert.equal(a.project.id, projectId(FAKE_MANIFEST.source));
});

await test("an unknown view is refused", () => {
  assert.throws(
    () => launchRequest({ target: "spreadsheet", manifest: FAKE_MANIFEST }),
    (err) => err instanceof LaunchError && err.code === "unknown_target",
  );
});

await test("a request must be bound to a manifest", () => {
  // An unbound request is the dangerous case: it would tell another application to open "the
  // project" with no agreement about which revision that is.
  assert.throws(
    () => launchRequest({ target: "studio", manifest: null }),
    (err) => err.code === "unbound",
  );
});

await test("a matching revision validates", () => {
  const req = launchRequest({ target: "studio", manifest: FAKE_MANIFEST });
  const v = validateLaunchRequest(req, {
    projectId: req.project.id, manifestId: FAKE_MANIFEST.manifestId, hash: "3".repeat(64),
  });
  assert.equal(v.ok, true);
  assert.equal(v.action, "open");
});

await test("a STALE revision is rejected, never reconciled", () => {
  // The requirement this test exists for: Verify must refuse rather than mutate another app or
  // do last-writer-wins.
  const req = launchRequest({ target: "studio", manifest: FAKE_MANIFEST });
  const v = validateLaunchRequest(req, {
    projectId: req.project.id, manifestId: FAKE_MANIFEST.manifestId, hash: "f".repeat(64),
  });
  assert.equal(v.ok, false);
  assert.equal(v.action, "reject", "a stale revision must never resolve to an open");
  assert.ok(v.findings.includes("stale_revision"));
  assert.match(v.reason, /does not reconcile/, "the refusal must say it will not reconcile");
});

await test("a mismatched manifest id is rejected", () => {
  const req = launchRequest({ target: "phi", manifest: FAKE_MANIFEST });
  const v = validateLaunchRequest(req, { manifestId: "0000000000000000" });
  assert.equal(v.action, "reject");
  assert.ok(v.findings.includes("manifest_mismatch"));
});

await test("a request for a different project is rejected", () => {
  const req = launchRequest({ target: "phi", manifest: FAKE_MANIFEST });
  const v = validateLaunchRequest(req, { projectId: "someoneelsesproject" });
  assert.equal(v.action, "reject");
  assert.ok(v.findings.includes("project_mismatch"));
});

await test("an unverifiable request is flagged, not waved through", () => {
  const req = launchRequest({
    target: "docs",
    manifest: { ...FAKE_MANIFEST, artifacts: [] },
  });
  assert.equal(req.revision.expectedHash, null);
  const v = validateLaunchRequest(req, {});
  assert.equal(v.ok, false, "no hash means nothing to compare, which is not a pass");
  assert.ok(v.findings.includes("no_expected_hash"));
});

await test("a future contract version is refused rather than guessed at", () => {
  const req = launchRequest({ target: "studio", manifest: FAKE_MANIFEST });
  const v = validateLaunchRequest({ ...req, contractVersion: 99 }, {});
  assert.equal(v.action, "reject");
  assert.ok(v.findings.includes("contract_version_mismatch"));
});

await test("a foreign payload is refused", () => {
  const v = validateLaunchRequest({ contract: "someone.else.launch" }, {});
  assert.equal(v.action, "reject");
  assert.ok(v.findings.includes("contract_unrecognised"));
});

await test("every target reports resolvability honestly", () => {
  // With nothing configured, nothing must claim to be resolvable -- a dead deep link is worse than
  // an honest "unavailable".
  const bare = launchTargets({ sourcePresent: false, env: {} });
  assert.equal(bare.length, 4);
  assert.deepEqual(bare.map((t) => t.id).sort(), ["docs", "phi", "source", "studio"]);
  for (const target of bare) {
    assert.equal(target.resolvable, false, `${target.id} claimed to resolve with nothing set up`);
    assert.ok(target.reason.length > 0, "an unavailable target must say why");
  }
  // And they become resolvable when the platform actually provides them.
  const wired = launchTargets({
    sourcePresent: true,
    env: { PHICAD_PLATFORM_LAUNCHER: "/opt/gateway/launcher", PHICAD_PLATFORM_DOCS: "https://x" },
  });
  assert.equal(wired.find((t) => t.id === "studio").resolvable, true);
  assert.equal(wired.find((t) => t.id === "phi").resolvable, true);
  assert.equal(wired.find((t) => t.id === "docs").resolvable, true);
  assert.equal(wired.find((t) => t.id === "source").resolvable, true);
});

await test("studio is a mode of the application that provides Verify, not a separate product",
           () => {
  // The architecture pivot: OpenPCB is dropped and PhiCad is now BOTH Studio and Verify. The
  // identifier `studio` is stable -- it names the view a caller wants -- but what stands behind it
  // is the native application, and a consumer must be able to tell that from the data alone.
  const req = launchRequest({ target: "studio", manifest: FAKE_MANIFEST });
  assert.equal(req.desiredView.target, "studio", "the identifier must not change");
  assert.equal(req.desiredView.sameApplicationAsVerify, true);
  assert.equal(req.desiredView.mode, "studio", "the intent is a MODE, not an application to start");
  assert.equal(req.desiredView.focus, "board-editor");

  const [studio] = launchTargets({ env: {} }).filter((x) => x.id === "studio");
  assert.match(studio.component, /native/i, "the component must name the native application");
  assert.equal(studio.sameApplicationAsVerify, true);
  assert.match(studio.description, /same installed application/i);

  // No dropped product and no deleted repository may appear in any target's wording.
  const wording = JSON.stringify(launchTargets({ env: {} })).toLowerCase();
  for (const gone of ["openpcb", "phicad-studio", "agpl"]) {
    assert.ok(!wording.includes(gone), `target metadata still references "${gone}"`);
  }
});

await test("the other views stay separate from the native application", () => {
  // Only studio is the same application. The thin web Circuit preview in particular is a separate
  // surface, and saying otherwise would imply an install that does not exist.
  for (const id of ["source", "phi", "docs"]) {
    const [target] = launchTargets({ env: {} }).filter((x) => x.id === id);
    assert.equal(target.sameApplicationAsVerify, false, `${id} must not claim to be this app`);
  }
});

await test("the web Circuit preview is unavailable until it is installed", () => {
  // With no source and no preview configured, the source view must refuse and SAY WHY.
  const [bare] = launchTargets({ sourcePresent: false, env: {} }).filter((x) => x.id === "source");
  assert.equal(bare.resolvable, false);
  assert.match(bare.reason, /no web Circuit preview surface is installed/);
  assert.match(bare.reason, /PHICAD_CIRCUIT_PREVIEW/, "an unavailable target must say how to fix it");

  // Installing the preview makes the view resolvable even with no local source.
  const [wired] = launchTargets({
    sourcePresent: false, env: { PHICAD_CIRCUIT_PREVIEW: "https://preview.example" },
  }).filter((x) => x.id === "source");
  assert.equal(wired.resolvable, true);
  assert.match(wired.reason, /web Circuit preview/);
});

await test("studio stays fail-closed until a launcher exists, and rejects a stale hash", () => {
  // Sharing a binary with Verify must NOT be mistaken for being reachable: a request still needs
  // something that can focus a view on a project.
  const [bare] = launchTargets({ env: {} }).filter((x) => x.id === "studio");
  assert.equal(bare.resolvable, false, "same application is not the same as reachable");
  assert.match(bare.reason, /native PhiCad application/);
  assert.match(bare.reason, /PHICAD_PLATFORM_LAUNCHER/);

  const [wired] = launchTargets({ env: { PHICAD_PLATFORM_LAUNCHER: "/opt/gateway/launcher" } })
    .filter((x) => x.id === "studio");
  assert.equal(wired.resolvable, true);

  // And being one application does not relax the manifest binding: an editor session opened earlier
  // can be holding older content, so a mismatch still rejects.
  const req = launchRequest({ target: "studio", manifest: FAKE_MANIFEST });
  const v = validateLaunchRequest(req, {
    projectId: req.project.id, manifestId: FAKE_MANIFEST.manifestId, hash: "e".repeat(64),
  });
  assert.equal(v.action, "reject");
  assert.ok(v.findings.includes("stale_revision"));
});

// --------------------------------------------------------------- apply: write discipline
//
// `test_apply.mjs` owns the apply contract itself. These are the hardening invariants: the ones
// that decide whether a *failure* can still damage a project or lie about what it did.

/** A scratch native project plus its Circuit JSON, for the apply invariants below. */
async function applyScratch(tag, elements) {
  const root = await mkdtemp(join(tmpdir(), `phicad-harden-apply-${tag}-`));
  const circuit = join(root, "circuit.json");
  await writeFile(circuit, JSON.stringify(elements ?? [
    { type: "pcb_board", pcb_board_id: "b", center: { x: 0, y: 0 }, width: 10, height: 10,
      thickness: 1.6, num_layers: 2 },
  ]));
  const project = join(root, "proj");
  await mkdir(project);
  await writeFile(join(project, "proj.kicad_pro"), "{}\n");
  await writeFile(join(project, "proj.kicad_pcb"), "(kicad_pcb (version 20260831))\n");
  return { root, circuit, project, plan: join(root, "plan.json"), result: join(root, "result.json") };
}

await test("apply never claims an undo entry on a status other than applied", async () => {
  // The specific lie this prevents: a caller reading `undo_entries: 1` off a rejected or
  // unavailable result and believing there is something to undo -- or that a write happened.
  const cases = [
    ["no history", {}],
    ["stale revision", { expectedRevision: "0".repeat(40) }],
    ["unknown element", { elements: [{ type: "pcb_hopes_and_dreams" }] }],
  ];
  for (const [label, options] of cases) {
    const s = await applyScratch("undo", options.elements);
    const r = await runApply({
      circuitJsonPath: s.circuit, projectPath: s.project, planPath: s.plan,
      resultPath: s.result, commit: true, expectedRevision: options.expectedRevision ?? null,
    });
    assert.notEqual(r.result.status, "applied", label);
    assert.equal(r.result.revision.undo_entries, 0, `${label}: no write means no undo entry`);
    assert.equal(r.result.revision.after, null, `${label}: no revision was created`);
    assert.ok(r.result.reason, `${label}: a non-applied status must say why`);
    assert.equal(r.exit_code, 3, label);
    await rm(s.root, { recursive: true, force: true });
  }
});

await test("apply refuses an oversized element count before it reads the project", async () => {
  // A 100k-element document is a denial of service against the translator, and the project must
  // not even be opened to find that out.
  const s = await applyScratch("toomany");
  // Deliberately the SHORTEST possible element, so the count limit is what fires rather than the
  // byte limit -- both refuse, but this test is about the count.
  const many = `[${new Array(APPLY_LIMITS.maxElements + 1).fill('{"type":"x"}').join(",")}]`;
  assert.ok(many.length < APPLY_LIMITS.maxSourceBytes, "stay under the byte limit on purpose");
  await writeFile(s.circuit, many);
  const r = await runApply({ circuitJsonPath: s.circuit, projectPath: s.project,
                             planPath: s.plan, resultPath: s.result, commit: true });
  assert.equal(r.result.status, "unavailable");
  assert.match(r.result.reason, /elements/);
  assert.equal(existsSync(s.plan), false, "nothing was planned, so no plan was written");
  await rm(s.root, { recursive: true, force: true });
});

await test("a cancelled apply is reported as cancelled, never as a plan", async () => {
  const s = await applyScratch("cancel");
  const controller = new AbortController();
  controller.abort();
  const r = await runApply({ circuitJsonPath: s.circuit, projectPath: s.project,
                            planPath: s.plan, resultPath: s.result, signal: controller.signal });
  assert.equal(r.result.status, "unavailable");
  assert.equal(r.plan, null, "a cancelled run has no plan to report");
  assert.equal(existsSync(s.plan), false);
  await rm(s.root, { recursive: true, force: true });
});

await test("the apply child inherits no credentials, only the toolchain pointers", async () => {
  // apply spawns python, which spawns the synth pipeline, which spawns kicad-cli. The env it
  // builds is an allowlist plus the two PHICAD_* pointers -- so a token in the caller's shell
  // cannot reach a board writer or a DRC run.
  process.env.PHICAD_APPLY_FAKE_TOKEN = "never-forward-me";
  try {
    const env = minimalEnv({ PHICAD_KICAD_CLI: "/x/kicad-cli" });
    assert.equal(env.PHICAD_APPLY_FAKE_TOKEN, undefined);
    assert.equal(env.PHICAD_KICAD_CLI, "/x/kicad-cli", "the explicit pointer is forwarded");
  } finally {
    delete process.env.PHICAD_APPLY_FAKE_TOKEN;
  }
});

await test("capabilities advertises the launch contract", async () => {
  const caps = await capabilities();
  assert.equal(caps.launch.contract, "io.github.couplet-physical-ai.phicad.launch");
  assert.equal(caps.launch.carriesCredentials, false);
  assert.equal(caps.launch.targets.length, 4);
  assert.match(caps.launch.note, /does not reconcile/);
});

console.log(`\n${passed} passed, ${failed} failed, ${skipped} skipped`);
process.exit(failed ? 1 : 0);
