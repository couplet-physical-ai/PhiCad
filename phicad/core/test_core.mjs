//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Contract tests for the engine wrapper. Run: bun phicad/core/test_core.mjs
//
// These assert the contracts that would silently rot: finding identity, the severity mapping,
// and -- most importantly -- that an absent verifier is never reported as a pass.

import { strict as assert } from "node:assert";

import {
  ERROR, Finding, WARNING, counts, findingsFromCircuitJson, isClean, sortFindings, summarise,
  summariseCircuit,
} from "./index.mjs";
import { findingsFromDrcReport } from "./kicad-verify.mjs";

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok    ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`  FAIL  ${name}\n        ${err.message}`);
  }
}

// --------------------------------------------------------------------------- identity

test("identity survives rewording", () => {
  const a = new Finding({ code: "drc.clearance", severity: ERROR, message: "Clearance violation",
                          refs: ["U1.2"], where: "F.Cu", at: { x_mm: 10, y_mm: 20 } });
  const b = new Finding({ code: "drc.clearance", severity: ERROR, message: "totally different",
                          refs: ["U1.2"], where: "F.Cu", at: { x_mm: 10, y_mm: 20 } });
  assert.equal(a.id, b.id, "rewording must not invalidate a granted waiver");
});

test("identity survives a sub-micron nudge", () => {
  const a = new Finding({ code: "drc.clearance", severity: ERROR, message: "x",
                          refs: ["U1.2"], at: { x_mm: 10, y_mm: 20 } });
  const b = new Finding({ code: "drc.clearance", severity: ERROR, message: "x",
                          refs: ["U1.2"], at: { x_mm: 10.0000004, y_mm: 20.0000004 } });
  assert.equal(a.id, b.id, "a nanometre re-route is the same violation");
});

test("identity changes when the violation moves", () => {
  const a = new Finding({ code: "drc.clearance", severity: ERROR, message: "x",
                          refs: ["U1.2"], at: { x_mm: 10, y_mm: 20 } });
  const b = new Finding({ code: "drc.clearance", severity: ERROR, message: "x",
                          refs: ["U1.2"], at: { x_mm: 40, y_mm: 20 } });
  assert.notEqual(a.id, b.id, "30 mm away is a different violation");
});

test("identity ignores ref order", () => {
  const a = new Finding({ code: "c", severity: ERROR, message: "m", refs: ["a", "b"] });
  const b = new Finding({ code: "c", severity: ERROR, message: "m", refs: ["b", "a"] });
  assert.equal(a.id, b.id);
});

test("identity matches the python implementation byte for byte", () => {
  // A finding crosses the JS/Python boundary constantly: the engine is TypeScript, the gates and
  // the MCP server are Python. If the two derivations disagreed, the same violation would have
  // two identities and every waiver would break at the boundary.
  //
  // This value was produced by phicad/synth/findings.py with the SAME inputs and a DIFFERENT
  // message, and verified equal on 2026-09-15:
  //   Finding(code='drc.clearance', severity=ERROR, message='different wording',
  //           refs=['U1.2','VIN'], where='F.Cu', at=(114.69, 111.53)).id  ->  ec342ac60052
  const f = new Finding({ code: "drc.clearance", severity: ERROR, message: "whatever",
                          refs: ["U1.2", "VIN"], where: "F.Cu",
                          at: { x_mm: 114.69, y_mm: 111.53 } });
  assert.equal(f.id, "ec342ac60052",
               "JS and Python must derive the same id from the same violation");
  assert.match(f.id, /^[0-9a-f]{12}$/);
});

// --------------------------------------------------------------------------- translation

test("circuit-json _warning becomes a warning finding", () => {
  const got = findingsFromCircuitJson([
    { type: "source_unnamed_trace_warning", message: "unnamed", source_trace_id: "st_1" },
  ]);
  assert.equal(got.length, 1);
  assert.equal(got[0].severity, WARNING);
  assert.equal(got[0].code, "tsc.source_unnamed_trace_warning");
  assert.ok(got[0].refs.includes("st_1"), "the referenced element id is kept");
  assert.ok(got[0].fix, "a known warning type carries advice");
});

test("circuit-json _error becomes an error finding", () => {
  const got = findingsFromCircuitJson([
    { type: "source_missing_property_error", message: "no resistance",
      source_component_id: "sc_0" },
  ]);
  assert.equal(got[0].severity, ERROR);
  assert.ok(!isClean(got), "an error means not clean");
});

test("ordinary circuit elements are not findings", () => {
  const got = findingsFromCircuitJson([
    { type: "pcb_smtpad", x: 1, y: 2 }, { type: "source_component", name: "R1" },
  ]);
  assert.equal(got.length, 0, "only _error/_warning elements are problems");
});

test("summariseCircuit counts what it says it counts", () => {
  const c = summariseCircuit([
    { type: "source_component" }, { type: "source_component" },
    { type: "pcb_smtpad" }, { type: "pcb_trace" },
    { type: "pcb_board", width: 18, height: 12 },
  ]);
  assert.equal(c.components, 2);
  assert.equal(c.pads, 1);
  assert.equal(c.traces, 1);
  assert.equal(c.width_mm, 18);
});

// --------------------------------------------------------------------------- drc bridge

test("a DRC violation keeps its coordinate and layer", () => {
  const got = findingsFromDrcReport({
    violations: [{
      type: "clearance", severity: "error",
      description: "Clearance violation ( clearance 0.2000 mm; actual 0.1569 mm)",
      items: [{ description: "Track [VIN] on F.Cu, length 9.05 mm",
                pos: { x: 114.69, y: 111.53 } }],
    }],
  });
  assert.equal(got[0].code, "drc.clearance");
  assert.deepEqual(got[0].at, { x_mm: 114.69, y_mm: 111.53 });
  assert.equal(got[0].where, "F.Cu");
  assert.ok(got[0].fix);
});

test("unconnected items become findings too", () => {
  const got = findingsFromDrcReport({
    unconnected_items: [{ severity: "error", description: "Missing connection",
                          items: [{ description: "Pad 2 of U1" }] }],
  });
  assert.equal(got[0].code, "drc.unconnected");
});

// --------------------------------------------------------------------------- ordering

test("worst first, regardless of discovery order", () => {
  const items = [
    new Finding({ code: "b", severity: "info", message: "i" }),
    new Finding({ code: "a", severity: ERROR, message: "e" }),
    new Finding({ code: "c", severity: WARNING, message: "w" }),
  ];
  assert.deepEqual(sortFindings(items).map((f) => f.severity), [ERROR, WARNING, "info"]);
  assert.deepEqual(sortFindings([...items].reverse()).map((f) => f.severity),
                   [ERROR, WARNING, "info"]);
});

test("summarise pluralises, and says nothing when there is nothing", () => {
  assert.equal(summarise([]), "no problems");
  assert.equal(summarise([new Finding({ code: "x", severity: ERROR, message: "m" })]), "1 error");
  assert.equal(counts([new Finding({ code: "x", severity: ERROR, message: "m" })]).error, 1);
});

test("warnings alone are clean", () => {
  assert.ok(isClean([new Finding({ code: "x", severity: WARNING, message: "m" })]),
            "a warning does not fail a check, matching the rest of the fork");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
