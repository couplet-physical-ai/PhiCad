#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""The apply service's write discipline, provable without a built kicad-cli.

Every test here asserts one of the two safety claims from the apply contract:

  * plan-only NEVER modifies the project — proven by hashing every project byte before and
    after, not by trusting the status field;
  * every rejection path leaves the project byte-identical.

The happy-path commit needs kicad-cli plus the kipy venv and lives in the JS suite
(`bun phicad/core/test_apply.mjs`) where the toolchain checks already exist.  These tests are
stdlib + git only, so they can run in `phicad check fast`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import circuit_apply  # noqa: E402
from test_circuit_json import fixture  # noqa: E402


def _tree_state(root: Path) -> dict:
    """Every file below `root` (excluding .git internals) and its content hash."""
    state = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if path.is_file():
            state[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


class ApplyServiceDiscipline(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="phicad-apply-test-"))
        self.circuit = self.root / "circuit.json"
        self.circuit.write_text(json.dumps(fixture()), encoding="utf-8")
        self.project = self.root / "proj"
        self.project.mkdir()
        (self.project / "proj.kicad_pro").write_text("{}\n", encoding="utf-8")
        (self.project / "proj.kicad_pcb").write_text(
            '(kicad_pcb (version 20260831) (generator "test"))\n', encoding="utf-8")
        self.plan = self.root / "plan.json"
        self.result = self.root / "result.json"

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        argv = [sys.executable, str(Path(circuit_apply.__file__)),
                "--circuit-json", str(self.circuit), "--project", str(self.project),
                "--plan", str(self.plan), "--result", str(self.result), *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)

    def _init_git(self) -> str:
        _git(self.project, "init", "-q", "-b", "main")
        _git(self.project, "config", "user.name", "test")
        _git(self.project, "config", "user.email", "test@example.invalid")
        _git(self.project, "add", ".")
        _git(self.project, "commit", "-qm", "baseline")
        return _git(self.project, "rev-parse", "HEAD")

    def test_plan_only_modifies_nothing(self):
        before = _tree_state(self.project)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(plan["schema"], "io.github.couplet-physical-ai.phicad.apply-plan/v1")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(_tree_state(self.project), before,
                         "plan-only must not change one byte of the project")

    def test_plan_json_is_canonical(self):
        self._run()
        raw = self.plan.read_bytes()
        self.assertTrue(raw.endswith(b"\n"), "the plan is newline-terminated")
        document = json.loads(raw)
        canonical = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
                     + "\n").encode("utf-8")
        self.assertEqual(raw, canonical, "the plan bytes are the canonical serialization")
        # The self-hash covers the document with plan_hash blanked.
        unhashed = dict(document, plan_hash="")
        expected = hashlib.sha256(
            (json.dumps(unhashed, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            .encode("utf-8")).hexdigest()
        self.assertEqual(document["plan_hash"], expected)

    def test_planning_twice_is_deterministic(self):
        self._run()
        first = self.plan.read_bytes()
        self._run()
        self.assertEqual(self.plan.read_bytes(), first,
                         "same bytes, same project state — same plan, byte for byte")

    def test_stale_expected_revision_rejects_and_writes_nothing(self):
        self._init_git()
        before = _tree_state(self.project)
        head_before = _git(self.project, "rev-parse", "HEAD")
        proc = self._run("--expected-revision", "0" * 40, "--commit")
        self.assertEqual(proc.returncode, 3)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "rejected")
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertIn("stale_revision", {r["code"] for r in plan["rejections"]})
        self.assertEqual(_tree_state(self.project), before)
        self.assertEqual(_git(self.project, "rev-parse", "HEAD"), head_before)

    def test_rejected_translation_never_reaches_the_writer(self):
        elements = fixture() + [{"type": "pcb_cutout", "center": {"x": 1, "y": 1},
                                 "width": 2, "height": 2}]
        self.circuit.write_text(json.dumps(elements), encoding="utf-8")
        self._init_git()
        before = _tree_state(self.project)
        proc = self._run("--commit")
        self.assertEqual(proc.returncode, 3)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "rejected")
        self.assertIn(("unsupported_required_geometry", "pcb_cutout"),
                      {(r["code"], r["element"]) for r in plan["rejections"]})
        self.assertEqual(_tree_state(self.project), before)

    def test_unknown_element_type_fails_closed_end_to_end(self):
        elements = fixture() + [{"type": "quantum_flux_capacitor"}]
        self.circuit.write_text(json.dumps(elements), encoding="utf-8")
        before = _tree_state(self.project)
        proc = self._run()
        self.assertEqual(proc.returncode, 3)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertIn(("unknown_element_type", "quantum_flux_capacitor"),
                      {(r["code"], r["element"]) for r in plan["rejections"]})
        self.assertEqual(_tree_state(self.project), before)

    def test_commit_without_history_is_unavailable(self):
        before = _tree_state(self.project)
        proc = self._run("--commit")
        self.assertEqual(proc.returncode, 3)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("Git", result["reason"])
        self.assertEqual(_tree_state(self.project), before)

    def test_commit_on_dirty_tree_is_unavailable(self):
        self._init_git()
        (self.project / "proj.kicad_pcb").write_text("(kicad_pcb (edited))\n", encoding="utf-8")
        before = _tree_state(self.project)
        proc = self._run("--commit")
        self.assertEqual(proc.returncode, 3)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("dirty", result["reason"])
        self.assertEqual(_tree_state(self.project), before)

    def test_non_finite_and_overflow_coordinates_reject(self):
        # 1e999 parses to Infinity in strict JSON readers and must be refused before
        # translation; 1e12 mm parses fine and must overflow the nm boundary.
        self.circuit.write_text('[{"type": "pcb_board", "center": {"x": 1e999, "y": 0}, '
                                '"width": 10, "height": 10, "thickness": 1.6}]',
                                encoding="utf-8")
        proc = self._run()
        self.assertEqual(proc.returncode, 3)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "rejected")
        codes = {r["code"] for r in plan["rejections"]}
        self.assertTrue({"invalid_circuit_json", "non_finite_coordinate"} & codes, codes)

        self.circuit.write_text(json.dumps([{
            "type": "pcb_board", "center": {"x": 1e12, "y": 0},
            "width": 10, "height": 10, "thickness": 1.6, "num_layers": 2,
        }]), encoding="utf-8")
        proc = self._run()
        self.assertEqual(proc.returncode, 3)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertIn("coordinate_overflow", {r["code"] for r in plan["rejections"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
