//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The artifact manifest: what a run produced, with which tools, and a digest of each file.
//
// WHY A MANIFEST RATHER THAN A LIST OF PATHS
// -----------------------------------------
// A caller that receives "here is your board" has no way to tell whether the file it is looking
// at came from the run it just asked for. Paths are reused, directories are stale, and a checker
// that reports a previous run's artifact as current is worse than one that reports nothing.
//
// So every artifact is recorded with its size, its sha256, and the versioned toolchain that
// produced it. Two runs of the same source with the same toolchain produce the same digests; a
// change in either shows up as a different manifest. That is what makes a result auditable
// instead of merely plausible.
//
// The `manifestId` is deterministic: it is derived from the source digest, the tool versions and
// the sorted artifact digests -- NOT from timestamps, paths or durations, which differ between
// two runs that produced byte-identical output.

import { createHash } from "node:crypto";
import { readFile, readdir, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import { basename, join, relative, sep } from "node:path";

import { LIMITS } from "./exec.mjs";
import { engineVersions } from "./toolchain.mjs";

async function sha256(path) {
  const buf = await readFile(path);
  return createHash("sha256").update(buf).digest("hex");
}

/**
 * A digest of a Circuit JSON artifact with the engine's RANDOM IDS REMOVED.
 *
 * The engine is non-deterministic in TWO ways, both measured on 2026-09-15 rather than assumed:
 *
 *   1. It mints a nanoid per diagnostic element, so two runs of one unchanged source produced
 *      byte-different circuit.json whose only diff was four `*_warning_id` values
 *      (`..._A7qmxxAf1C` vs `..._x13kadXfni`).
 *
 *   2. DIAGNOSTIC ORDER VARIES BETWEEN RUNS. Parts are resolved concurrently, so the array order
 *      of `supplier_footprint_mismatch_warning` elements follows whichever supplier lookup
 *      finished first: element 52 was R1/C11702 in one run and D1/C965799 in the next. Stripping
 *      ids alone left the manifest unstable, which a first attempt at this test missed because it
 *      used synthetic single-element fixtures.
 *
 * So identity is computed over the artifact with generated ids stripped AND elements sorted by a
 * canonical form. Two runs of the same source and toolchain now agree, and any REAL change -- a
 * moved pad, a different footprint, an added or removed diagnostic -- still changes the digest,
 * because sorting reorders elements without discarding any.
 *
 * The raw digest is kept alongside as `sha256`, because reproducing an exact byte stream is a
 * different question from reproducing a design, and both are worth being able to ask.
 */
const GENERATED_ID = /^[a-z0-9_]+_id$/;

function stripGeneratedIds(value) {
  if (Array.isArray(value)) return value.map(stripGeneratedIds);
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) {
      if (GENERATED_ID.test(key)) continue;
      out[key] = stripGeneratedIds(value[key]);
    }
    return out;
  }
  return value;
}

async function contentDigest(path) {
  if (!path.endsWith(".json")) return null;
  try {
    const parsed = JSON.parse(await readFile(path, "utf8"));
    const stripped = stripGeneratedIds(parsed);
    // Sort a top-level element array into a canonical order. Element COUNT and CONTENT are
    // preserved -- only the arbitrary concurrency-dependent order is normalised.
    const canonical = Array.isArray(stripped)
      ? [...stripped].map((e) => JSON.stringify(e)).sort()
      : stripped;
    return createHash("sha256").update(JSON.stringify(canonical)).digest("hex");
  } catch {
    return null;   // not JSON, or unreadable: the raw digest still covers it
  }
}

/** Every file under a directory, excluding the engine's own cache, bounded by file count. */
async function walk(dir) {
  if (!existsSync(dir)) return [];
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
    if (full.includes(`${sep}.tscircuit${sep}`)) continue; // parts cache, machine-local
    out.push(full);
  }
  return out;
}

/**
 * Build a manifest for one run.
 *
 * `extra` carries the tool-specific provenance the callers already have (durations, exit codes,
 * kicad-cli version), which is recorded but deliberately excluded from `manifestId`.
 */
export async function buildManifest({
  sourcePath,
  outDir,
  artifacts = null,
  tools = {},
  extra = {},
} = {}) {
  const files = artifacts ?? (outDir ? await walk(outDir) : []);
  const recorded = [];
  let truncated = false;

  for (const file of files) {
    if (recorded.length >= LIMITS.maxArtifactFiles) { truncated = true; break; }
    const info = await stat(file).catch(() => null);
    if (!info) continue;
    recorded.push({
      path: file,
      name: outDir ? relative(outDir, file) : basename(file),
      bytes: info.size,
      // A file over the limit is recorded WITHOUT a digest rather than silently omitted: the
      // caller must be able to see that something oversized exists.
      sha256: info.size <= LIMITS.maxArtifactBytes ? await sha256(file) : null,
      contentSha256: info.size <= LIMITS.maxArtifactBytes ? await contentDigest(file) : null,
      oversized: info.size > LIMITS.maxArtifactBytes,
    });
  }

  recorded.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));

  const sourceDigest = sourcePath && existsSync(sourcePath) ? await sha256(sourcePath) : null;
  const toolVersions = { engine: engineVersions(), ...tools };

  // Deterministic identity: source + tools + artifact digests. No paths, no times.
  const identity = JSON.stringify({
    source: sourceDigest,
    tools: toolVersions,
    // contentSha256 when we have one (engine-generated ids removed), else the raw digest.
    artifacts: recorded.map((a) => [a.name, a.contentSha256 ?? a.sha256]),
  });

  return {
    product: "PhiCad Verify",
    manifestId: createHash("sha256").update(identity).digest("hex").slice(0, 16),
    source: sourcePath ?? null,
    sourceSha256: sourceDigest,
    outDir: outDir ?? null,
    tools: toolVersions,
    artifacts: recorded,
    artifactCount: recorded.length,
    truncated,
    // Circuit JSON is a build/interchange boundary, not authored source. Recorded explicitly so a
    // consumer does not treat an exported artifact as something it can edit and feed back in.
    boundary: {
      authoredSource: sourcePath ?? null,
      interchange: "circuit-json",
      lossless: false,
      note: "Circuit JSON is a versioned build output of one toolchain, not authored source; "
            + "round-tripping it back to TSX is not supported.",
    },
    ...extra,
  };
}
