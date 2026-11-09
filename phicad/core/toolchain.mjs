//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Which binaries exist, where exactly they are, and what version they are.
//
// Every result PhiCad Verify reports carries the provenance of the tool that produced it. That
// is not bookkeeping: a finding without a tool version cannot be reproduced, and two runs of
// "the same" check against different tscircuit builds are not comparable. A missing binary is
// reported as a missing binary -- never silently downloaded, and never treated as a pass.

import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { minimalEnv, run } from "./exec.mjs";
import { LAUNCH_CONTRACT_VERSION, launchTargets } from "./launch.mjs";

export const CORE_DIR = dirname(fileURLToPath(import.meta.url));

/**
 * The tscircuit CLI, as installed by the committed lockfile.
 *
 * node_modules/.bin/tsci is a symlink into node_modules/tscircuit, so this is the exact build
 * bun installed -- not whatever `bunx tsci` would resolve or fetch at run time.
 */
export function tsciBinary() {
  const local = join(CORE_DIR, "node_modules", ".bin", "tsci");
  return existsSync(local) ? local : null;
}

/** The JS runtime that executes the CLI. Absolute, so PATH order cannot change what runs. */
export function bunBinary() {
  for (const candidate of [process.env.PHICAD_BUN, "/opt/homebrew/bin/bun", "/usr/local/bin/bun"]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

/** This fork's kicad-cli. PHICAD_KICAD_CLI wins, as everywhere else in the fork. */
export function kicadCliBinary() {
  const fromEnv = process.env.PHICAD_KICAD_CLI;
  if (fromEnv && existsSync(fromEnv)) return fromEnv;
  const build = process.env.PHICAD_BUILD;
  if (build) {
    const guess = join(build, "kicad", "PhiCad.app", "Contents", "MacOS", "kicad-cli");
    if (existsSync(guess)) return guess;
  }
  return null;
}

function packageVersion(name) {
  try {
    const p = join(CORE_DIR, "node_modules", ...name.split("/"), "package.json");
    return JSON.parse(readFileSync(p, "utf8")).version ?? null;
  } catch {
    return null;
  }
}

/** Locked dependency versions, read from what is actually installed. */
export function engineVersions() {
  return {
    tscircuit: packageVersion("tscircuit"),
    "@tscircuit/eval": packageVersion("@tscircuit/eval"),
    "circuit-json": packageVersion("circuit-json"),
  };
}

/**
 * `bun --version`, cached for the process.
 *
 * The version lock has to be machine-verifiable, so every entry must be a version STRING. A path
 * is not a version -- two machines can have the same path and different binaries.
 */
let bunVersionCache;

export async function bunVersion({ signal = null } = {}) {
  if (bunVersionCache !== undefined) return bunVersionCache;
  const bin = bunBinary();
  if (!bin) { bunVersionCache = null; return null; }
  try {
    const { stdout } = await run(bin, ["--version"], { timeoutMs: 30_000, signal });
    bunVersionCache = stdout.trim().split("\n")[0] || null;
  } catch {
    bunVersionCache = null;
  }
  return bunVersionCache;
}

/**
 * `kicad-cli --version`, cached for the process.
 *
 * Bounded like every other child: a checker that hangs while asking a tool its version is worse
 * than one that reports the version as unknown.
 */
let kicadVersionCache;
export async function kicadVersion({ signal = null } = {}) {
  if (kicadVersionCache !== undefined) return kicadVersionCache;
  const cli = kicadCliBinary();
  if (!cli) { kicadVersionCache = null; return null; }
  try {
    const r = await run(cli, ["--version"], {
      cwd: CORE_DIR,
      env: minimalEnv({ KICAD_RUN_FROM_BUILD_DIR: "1" }),
      timeoutMs: 30_000,
      signal,
    });
    kicadVersionCache = (r.stdout || r.stderr).trim().split("\n")[0] || null;
  } catch {
    kicadVersionCache = null;
  }
  return kicadVersionCache;
}

/**
 * What this installation can actually do, with no side effects.
 *
 * This is the honest answer to "is PhiCad Verify usable here", and it is what the MCP
 * `verify_capabilities` tool returns. Each capability is a checked fact, not a claim.
 */
export async function capabilities({ signal = null } = {}) {
  const bun = bunBinary();
  const tsci = tsciBinary();
  const kicad = kicadCliBinary();
  return {
    product: "PhiCad Verify",
    role: "independent CAD check over tscircuit build output",
    harness: "phi (the only AI harness; PhiCad Verify contains no model runtime, "
             + "no provider UI and no credentials)",
    engine: {
      available: Boolean(bun && tsci),
      runtime: bun,
      cli: tsci,
      versions: engineVersions(),
      note: tsci ? "locked local binary from the committed lockfile"
                 : "tscircuit CLI not installed — run `bun install` in phicad/core",
    },
    checker: {
      available: Boolean(kicad),
      cli: kicad,
      version: await kicadVersion({ signal }),
      note: kicad ? "KiCad CAD check only; not a fabrication-readiness verdict"
                  : "kicad-cli not found — build the fork or set PHICAD_KICAD_CLI",
    },
    exports: {
      note: "tscircuit export targets are available through the engine. Manufacturing export is "
            + "the native PhiCad application's job -- the same application that provides Verify -- "
            + "through kicad-cli (gerbers, drill, ODB++, IPC-2581, position files).",
    },
    // Cross-view navigation. Verify EMITS a launch request and never opens another application
    // itself; the platform launcher owns that. Each target reports whether it actually resolves
    // on this machine, because a deep link that silently fails is worse than one that admits it
    // cannot resolve.
    launch: {
      contract: "io.github.couplet-physical-ai.phicad.launch",
      contractVersion: LAUNCH_CONTRACT_VERSION,
      carriesCredentials: false,
      note: "Verify emits a manifest-bound launch request; it does not open other applications "
            + "and does not reconcile revisions. A stale or mismatched hash is rejected.",
      targets: launchTargets({ sourcePresent: false }),
    },
  };
}
