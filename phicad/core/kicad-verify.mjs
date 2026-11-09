//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
// Copyright The KiCad Developers, see AUTHORS.txt for contributors.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// PhiCad Verify's CAD check: export a tscircuit build to .kicad_pcb, run KiCad's DRC over
// it, and return the result as PhiCad findings.
//
// WHAT THIS IS, AND WHAT IT IS NOT
// -------------------------------
// This is an INDEPENDENT CAD CHECK. KiCad's DRC evaluates the board against the design rules
// present in the file it is given -- clearances, widths, hole sizes, connectivity. That is a
// different engine reading the exported artifact, with no knowledge of the TSX that produced it,
// which is what makes it worth running at all.
//
// It is NOT:
//   * a fabrication-readiness verdict. No fabricator's process, capability or acceptance
//     criteria are consulted. A DRC-clean board can still be unmanufacturable, and a board with
//     violations may be perfectly buildable at a given house.
//   * a manufacturing export. That is `kicad-cli pcb export` in the same native PhiCad
//     application, a separate step from checking, and this module does not run it.
//   * a statement about the tscircuit engine's correctness. The two sets of findings are kept
//     separate and never merged, because a tool's opinion of its own output is not an independent
//     check.
//
// A worked example, reproduced on 2026-09-15: a 2-component TSX board exported to .kicad_pcb and
// checked with this fork's kicad-cli 10.99.0 reported a 0.1569 mm clearance against the 0.2 mm
// minimum recorded in that file, plus three tracks under the recorded minimum width. tscircuit
// had built the same board successfully under its own rules. Both statements are accurate; they
// answer different questions.
//
// KiCad is a trademark of the KiCad project; this fork's attribution and licence notices are
// preserved unchanged.

import { existsSync } from "node:fs";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { ExecError, LIMITS, minimalEnv, run } from "./exec.mjs";
import { ERROR, Finding, WARNING, exportCircuit, sortFindings } from "./index.mjs";
import { kicadCliBinary, kicadVersion } from "./toolchain.mjs";

/** Fixes worth stating; the rest of KiCad's messages are already specific. */
const DRC_FIX = {
  clearance: "widen the gap, or lower the clearance for this net class",
  track_width: "widen the track, or lower the minimum width for this net class",
  shorting_items: "move one of the two items apart, or put them on the same net",
  solder_mask_bridge: "increase the mask expansion, or move the items apart",
  hole_clearance: "move the hole, or reduce its diameter",
  courtyards_overlap: "move the footprints apart",
  silk_over_copper: "move the silkscreen text off the pad",
  unconnected_items: "route the connection, or declare it no-connect if it is deliberate",
};

const LAYERS = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "F.SilkS", "B.SilkS", "Edge.Cuts"];

function layerOf(items) {
  for (const item of items) {
    for (const layer of LAYERS) if ((item.description ?? "").includes(layer)) return layer;
  }
  return "";
}

/** KiCad's DRC JSON -> PhiCad findings, keeping the geometry that produced each violation. */
export function findingsFromDrcReport(report) {
  const out = [];
  for (const v of report.violations ?? []) {
    const items = v.items ?? [];
    let at = null;
    for (const item of items) {
      if (typeof item?.pos?.x === "number" && typeof item?.pos?.y === "number") {
        at = { x_mm: item.pos.x, y_mm: item.pos.y };
        break;
      }
    }
    out.push(new Finding({
      code: `drc.${v.type ?? "unknown"}`,
      severity: v.severity === "warning" ? WARNING : ERROR,
      message: v.description ?? v.type ?? "DRC violation",
      refs: items.map((i) => i.description).filter(Boolean).slice(0, 4),
      where: layerOf(items),
      at,
      fix: DRC_FIX[v.type] ?? "",
    }));
  }
  for (const u of report.unconnected_items ?? []) {
    const items = u.items ?? [];
    out.push(new Finding({
      code: "drc.unconnected",
      severity: u.severity === "warning" ? WARNING : ERROR,
      message: u.description ?? "Missing connection between items",
      refs: items.map((i) => i.description).filter(Boolean).slice(0, 4),
      fix: DRC_FIX.unconnected_items,
    }));
  }
  return sortFindings(out);
}

/**
 * Export a circuit and run KiCad's DRC over it.
 *
 * Returns `{ ok, skipped, board, findings, manifest }`.
 *
 * `skipped` is true only when kicad-cli is genuinely absent, and it is NEVER reported as a pass:
 * an unavailable checker is not a verdict. That distinction is the entire subject of the
 * CHECK_BROKEN work elsewhere in this fork -- a gate that cannot run must not look like a gate
 * that ran and was satisfied.
 */
export async function verifyWithKicad(tsxPath, {
  boardPath = null,
  signal = null,
  timeoutMs = LIMITS.timeoutMs,
} = {}) {
  const cli = kicadCliBinary();
  if (!cli) {
    return {
      ok: false,
      skipped: true,
      board: null,
      findings: [new Finding({
        code: "verify.kicad_cli_missing",
        severity: WARNING,
        message: "kicad-cli was not found, so no independent CAD check was performed",
        fix: "build the fork (phicad build kicad-cli) or set PHICAD_KICAD_CLI",
      })],
      manifest: { tool: "kicad-cli", version: null, skipped: true },
    };
  }

  const dir = await mkdtemp(join(tmpdir(), "phicad-verify-drc-"));
  const board = boardPath ?? join(dir, "board.kicad_pcb");
  const exported = await exportCircuit(tsxPath, "kicad_pcb", board, { signal, timeoutMs });
  if (!exported.ok) {
    return {
      ok: false,
      skipped: false,
      board: null,
      findings: [new Finding({
        code: "verify.export_failed",
        severity: ERROR,
        message: exported.message || "the engine produced no .kicad_pcb to check",
        refs: [tsxPath],
        fix: "check that the design builds before asking for a CAD check",
      })],
      manifest: { tool: "kicad-cli", version: await kicadVersion({ signal }) },
    };
  }

  const reportPath = join(dir, "drc.json");
  const startedAt = Date.now();
  let exec;
  try {
    // `kicad-cli pcb drc` EXITS 0 EVEN WITH VIOLATIONS, so the exit status is deliberately not
    // used as the verdict anywhere below; the report is the only truth. This is a documented trap
    // in the fork's own docs and has caused wrong verdicts here before.
    exec = await run(cli, ["pcb", "drc", "--format", "json", "--severity-all", "--refill-zones",
                           "-o", reportPath, resolve(board)], {
      cwd: dir,
      env: minimalEnv({ KICAD_RUN_FROM_BUILD_DIR: "1" }),
      timeoutMs,
      signal,
    });
  } catch (err) {
    const code = err instanceof ExecError ? err.code : "exit";
    return {
      ok: false,
      skipped: false,
      board,
      findings: [new Finding({
        code: code === "timeout" ? "verify.drc_timeout"
             : code === "cancelled" ? "verify.cancelled" : "verify.drc_failed",
        severity: ERROR,
        message: err.message,
        fix: code === "timeout" ? "raise the timeout, or reduce the board's complexity"
                                : "run the kicad-cli command by hand to see what it printed",
      })],
      manifest: { tool: "kicad-cli", version: await kicadVersion({ signal }), failure: code },
    };
  }

  // Only accept a report written by THIS run: a stale drc.json would describe a different board.
  const info = existsSync(reportPath) ? await stat(reportPath) : null;
  if (!info || info.mtimeMs < startedAt - 1000) {
    return {
      ok: false,
      skipped: false,
      board,
      findings: [new Finding({
        code: info ? "verify.stale_drc_report" : "verify.drc_produced_no_report",
        severity: ERROR,
        message: info
          ? "the DRC report on disk predates this run and was rejected as stale"
          : `kicad-cli exited ${exec.code} but wrote no DRC report, so the board is unchecked`,
        fix: "run the kicad-cli command by hand to see what it printed",
      })],
      manifest: { tool: "kicad-cli", version: await kicadVersion({ signal }) },
    };
  }
  if (info.size > LIMITS.maxArtifactBytes) {
    return {
      ok: false, skipped: false, board,
      findings: [new Finding({
        code: "verify.drc_report_too_large",
        severity: ERROR,
        message: `the DRC report is ${info.size} bytes, over the ${LIMITS.maxArtifactBytes} limit`,
        fix: "reduce the board's complexity, or raise the limit deliberately",
      })],
      manifest: { tool: "kicad-cli", version: await kicadVersion({ signal }) },
    };
  }

  let report;
  try {
    report = JSON.parse(await readFile(reportPath, "utf8"));
  } catch (err) {
    return {
      ok: false, skipped: false, board,
      findings: [new Finding({
        code: "verify.drc_report_unreadable",
        severity: ERROR,
        message: `the DRC report could not be parsed: ${err.message}`,
      })],
      manifest: { tool: "kicad-cli", version: await kicadVersion({ signal }) },
    };
  }

  const findings = findingsFromDrcReport(report);
  return {
    ok: !findings.some((f) => f.severity === ERROR),
    skipped: false,
    board,
    findings,
    manifest: {
      tool: "kicad-cli",
      version: await kicadVersion({ signal }),
      cli,
      report: reportPath,
      board,
      durationMs: exec.durationMs,
      exitCode: exec.code,
      note: "independent CAD check against the rules recorded in the exported board; "
            + "not a fabrication-readiness verdict",
    },
  };
}
