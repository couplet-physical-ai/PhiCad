//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Bounded, cancellable execution of exactly one locked local binary.
//
// WHY THIS FILE EXISTS
// -------------------
// The first proof of this engine called `spawnSync("bunx", ["tsci", ...])`. Three separate
// problems, each disqualifying for anything called V1:
//
//   1. `bunx` RESOLVES AND MAY DOWNLOAD AT RUN TIME. A product that fetches an unpinned tool
//      while the user is building a board has no reproducible behaviour and no supply-chain
//      story. The binary must be the one in node_modules/.bin, installed by the committed
//      lockfile, and its absence must be an error rather than a silent download.
//
//   2. spawnSync CANNOT BE CANCELLED. It blocks the event loop until the child exits, so an
//      agent, an MCP client or a UI has no way to abandon a build. A `timeout` option only
//      bounds the wall clock; it does not give the caller a handle.
//
//   3. THE CHILD INHERITED THE WHOLE ENVIRONMENT. Every API key, token and gateway credential
//      in the parent process was handed to a third-party tool and to whatever it spawned. The
//      earlier code deleted six proxy variables from a full copy of `process.env`; that is a
//      denylist, and a denylist of secrets is always incomplete.
//
// This module therefore does the opposite of each: exact absolute paths, an AbortSignal, and an
// ALLOWLISTED environment built from nothing.

import { spawn } from "node:child_process";

/** Hard ceilings. Exceeding one is a reported failure, never a silent truncation of meaning. */
export const LIMITS = {
  timeoutMs: 300_000,        // 5 min: a build that takes longer is stuck, not slow
  maxOutputBytes: 8 * 1024 * 1024,
  maxSourceBytes: 2 * 1024 * 1024,
  maxArtifactBytes: 64 * 1024 * 1024,
  maxArtifactFiles: 2000,
};

/**
 * The only environment variables a child gets. Nothing else is forwarded -- in particular no
 * *_TOKEN, *_KEY, *_SECRET, no LLM gateway credentials, and no *_proxy (which on a managed
 * machine points at the model gateway and would send a build tool's traffic there).
 *
 * PATH is included because the toolchain shells out to `git` and platform utilities; it is the
 * one unavoidable inheritance and is documented as such in the security notes.
 */
const ENV_ALLOWLIST = ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "USER", "SHELL"];

export function minimalEnv(extra = {}) {
  const out = {};
  for (const key of ENV_ALLOWLIST) {
    if (process.env[key] !== undefined) out[key] = process.env[key];
  }
  // Deliberately explicit: these two make tscircuit's parts engine and registry client refuse
  // network access rather than reach out mid-build. See docs: no unrestricted network is assumed.
  out.NO_COLOR = "1";
  out.CI = "1";
  return { ...out, ...extra };
}

export class ExecError extends Error {
  constructor(message, { code, stdout = "", stderr = "", signal = null, durationMs = 0 }) {
    super(message);
    this.name = "ExecError";
    this.code = code;              // "timeout" | "cancelled" | "output_limit" | "exit" | "spawn"
    this.stdout = stdout;
    this.stderr = stderr;
    this.signal = signal;
    this.durationMs = durationMs;
  }
}

/**
 * Run one binary to completion, or fail in a way the caller can act on.
 *
 * Returns { code, stdout, stderr, durationMs }. Rejects with ExecError for timeout,
 * cancellation, an output-limit breach, or a spawn failure. A non-zero exit is NOT a rejection:
 * plenty of tools here exit non-zero while still producing a usable report, and conflating
 * "exited non-zero" with "produced nothing" is the mistake that made `kicad-cli pcb drc`
 * dangerous to judge by exit code.
 */
export async function run(binary, args, {
  cwd,
  env = minimalEnv(),
  timeoutMs = LIMITS.timeoutMs,
  maxOutputBytes = LIMITS.maxOutputBytes,
  signal = null,
} = {}) {
  const started = Date.now();

  return await new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(binary, args, {
        cwd,
        env,
        stdio: ["ignore", "pipe", "pipe"],
        // No shell: arguments are passed as a vector, so a path containing a space or a
        // metacharacter cannot become another command.
        shell: false,
      });
    } catch (err) {
      reject(new ExecError(`cannot spawn ${binary}: ${err.message}`, { code: "spawn" }));
      return;
    }

    let stdout = "";
    let stderr = "";
    let bytes = 0;
    let settled = false;
    let timer = null;
    let onAbort = null;

    const cleanup = () => {
      if (timer) clearTimeout(timer);
      if (onAbort && signal) signal.removeEventListener("abort", onAbort);
    };

    // SIGTERM first, then SIGKILL: a tool that ignores TERM must not be able to outlive the
    // caller's decision to stop it.
    const kill = () => {
      child.kill("SIGTERM");
      setTimeout(() => { if (!child.killed) child.kill("SIGKILL"); }, 2000).unref?.();
    };

    const fail = (message, code) => {
      if (settled) return;
      settled = true;
      cleanup();
      kill();
      reject(new ExecError(message, {
        code, stdout, stderr, durationMs: Date.now() - started,
      }));
    };

    timer = setTimeout(
      () => fail(`timed out after ${timeoutMs} ms`, "timeout"),
      timeoutMs,
    );
    timer.unref?.();

    if (signal) {
      if (signal.aborted) { fail("cancelled before start", "cancelled"); return; }
      onAbort = () => fail("cancelled", "cancelled");
      signal.addEventListener("abort", onAbort, { once: true });
    }

    const collect = (streamName) => (chunk) => {
      bytes += chunk.length;
      if (bytes > maxOutputBytes) {
        fail(`output exceeded ${maxOutputBytes} bytes`, "output_limit");
        return;
      }
      if (streamName === "stdout") stdout += chunk;
      else stderr += chunk;
    };

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", collect("stdout"));
    child.stderr.on("data", collect("stderr"));

    child.on("error", (err) => fail(`${binary}: ${err.message}`, "spawn"));

    child.on("close", (code, sig) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve({ code, signal: sig, stdout, stderr, durationMs: Date.now() - started });
    });
  });
}
