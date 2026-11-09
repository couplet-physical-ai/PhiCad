#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Offline tests for the parts backends: argv, log parsing, fallback decision.

No network and no installed backend: the subprocess runner is faked with real
captured logs, and the executable lookup is faked with a set of "installed"
backends.  What is pinned here is the contract the pipeline depends on --
JLC2KiCadLib must be told to write the layout audit_parts.py can resolve, and the
AGPL fallback must never be chosen silently.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import fetch_part


# Captured from JLC2KiCadLib 1.3.2 (C14663, logging to stderr).  Note the three
# quirks the wrapper absorbs: the model path goes through the .pretty directory,
# the stored binding is $(KIPRJMOD)-relative to that directory, and the symbol is
# logged as "..._0" while the name written to the library has no suffix.
JLC_LOG = """\
2026-09-22 12:39:54,913 - INFO - creating library for component C14663
2026-09-22 12:39:55,134 - INFO - Creating footprint ...
2026-09-22 12:39:55,358 - INFO - Downloading STEP Model ...
2026-09-22 12:39:55,526 - INFO - STEP model created at {lib}/project.pretty/../project.3dshapes/C0603.step
2026-09-22 12:39:55,526 - INFO - added "$(KIPRJMOD)/../project.3dshapes/C0603.step" to footprint
2026-09-22 12:39:55,527 - INFO - Creating WRL model ...
2026-09-22 12:39:55,741 - INFO - WRL model created at {{filename}}
2026-09-22 12:39:55,742 - INFO - Created '{lib}/project.pretty/C0603.kicad_mod'
2026-09-22 12:39:55,962 - INFO - Creating symbol CC0603KRX7R9BB104_0 in project
2026-09-22 12:39:55,963 - INFO - writing in {lib}/./project.kicad_sym file
"""

# Captured from JLC2KiCadLib 1.3.2 for an id that does not exist: exit code 0.
JLC_UNKNOWN_ID_LOG = """\
2026-09-22 12:37:22,435 - INFO - creating library for component C99999999
2026-09-22 12:37:22,651 - ERROR - failed to get component uuid for C99999999
The component # is probably wrong. Check a possible typo and that the component exists on easyEDA
"""

# Captured from easyeda2kicad 1.0.1 (C14663).
EASYEDA_LOG = """\
[INFO] Created Kicad symbol for ID : C14663
       Symbol name : CC0603KRX7R9BB104
       Library path : {lib}/project.kicad_sym
[INFO] Created Kicad footprint for ID: C14663
       Footprint name: C0603
       Footprint path: {lib}/project.pretty/C0603.kicad_mod
[INFO] Created 3D model for ID: C14663
       3D model name: C0603_L1.6-W0.8-H0.8
       3D model path (wrl): {lib}/project.3dshapes/C0603_L1.6-W0.8-H0.8.wrl
       3D model path (step): {lib}/project.3dshapes/C0603_L1.6-W0.8-H0.8.step
-- easyeda2kicad.py v1.0.1 --
"""

SYMBOL_LIB = """\
(kicad_symbol_lib (version 20210201) (generator TousstNicolas/JLC2KiCad_lib)
  (symbol "CC0603KRX7R9BB104" (pin_names hide) (in_bom yes) (on_board yes)
    (symbol "CC0603KRX7R9BB104_0_1"
      (pin passive line (at -3.81 0 0) (length 2.54))
    )
  )
)
"""

FOOTPRINT = """\
(footprint "C0603" (version 20221018) (generator JLC2KiCad_lib)
  (pad "1" smd roundrect (at -0.7875 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask"))
  (model "$(KIPRJMOD)/../project.3dshapes/C0603.step"
    (offset (xyz 0 0 0))
    (scale (xyz 1 1 1))
    (rotate (xyz 0 0 0))
  )
)
"""


class FakeRunner:
    """subprocess.run stand-in: records argv, returns a canned CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.kwargs.append(kwargs)
        return self.result


def fake_finder(*installed: str):
    """find_backend_exe stand-in: only `installed` backends have an executable."""
    return lambda backend: f"/fake/venv/bin/{fetch_part.BACKENDS[backend][0]}" \
        if backend in installed else None


class BackendChoiceTests(unittest.TestCase):
    def test_default_is_the_mit_backend(self):
        backend, exe, note = fetch_part.choose_backend(
            None, find=fake_finder("jlc2kicad", "easyeda2kicad"))

        self.assertEqual(backend, "jlc2kicad")
        self.assertEqual(exe, "/fake/venv/bin/JLC2KiCadLib")
        self.assertIsNone(note)

    def test_fallback_warns_and_names_the_licence(self):
        backend, exe, note = fetch_part.choose_backend(
            None, find=fake_finder("easyeda2kicad"))

        self.assertEqual(backend, "easyeda2kicad")
        self.assertEqual(exe, "/fake/venv/bin/easyeda2kicad")
        self.assertIn("AGPL-3.0", note)
        self.assertIn("one release", note)

    def test_explicit_backend_is_never_swapped(self):
        backend, exe, note = fetch_part.choose_backend(
            "jlc2kicad", find=fake_finder("easyeda2kicad"))

        self.assertIsNone(backend)
        self.assertIsNone(exe)
        self.assertIn("not installed", note)
        self.assertIn("pip install JLC2KiCadLib", note)

    def test_explicit_fallback_is_honoured_without_a_warning(self):
        backend, _exe, note = fetch_part.choose_backend(
            "easyeda2kicad", find=fake_finder("jlc2kicad", "easyeda2kicad"))

        self.assertEqual(backend, "easyeda2kicad")
        self.assertIsNone(note)

    def test_nothing_installed_is_an_error_with_one_install_command(self):
        backend, _exe, note = fetch_part.choose_backend(None, find=fake_finder())

        self.assertIsNone(backend)
        self.assertIn("no parts backend installed", note)
        self.assertEqual(note.count("pip install "), 1)


class ArgumentTests(unittest.TestCase):
    def test_jlc2kicadlib_argv_targets_the_project_library_layout(self):
        cmd = fetch_part.jlc2kicadlib_command(
            "/fake/venv/bin/JLC2KiCadLib", Path("/proj"), "project", "C14663")
        pairs = dict(zip(cmd, cmd[1:]))

        self.assertEqual(cmd[:2], ["/fake/venv/bin/JLC2KiCadLib", "C14663"])
        self.assertEqual(pairs["-dir"], "/proj/lib")
        self.assertEqual(pairs["-symbol_lib"], "project")
        self.assertEqual(pairs["-symbol_lib_dir"], ".")
        self.assertEqual(pairs["-footprint_lib"], "project.pretty")
        self.assertEqual(pairs["-model_dir"], "../project.3dshapes")
        self.assertEqual(pairs["-model_base_variable"], "KIPRJMOD")
        self.assertEqual(cmd[cmd.index("-models") + 1:cmd.index("-models") + 3],
                         ["STEP", "WRL"])

    def test_easyeda2kicad_argv_is_unchanged(self):
        cmd = fetch_part.easyeda2kicad_command(
            "easyeda2kicad", Path("/proj"), "mylib", "C6186")

        self.assertEqual(cmd, ["easyeda2kicad", "--full", "--overwrite",
                               "--project-relative", "--lcsc_id", "C6186",
                               "--output", "/proj/lib/mylib.kicad_sym"])

    def test_proxy_vars_are_stripped_from_the_child_env(self):
        import os
        saved = os.environ.get("HTTPS_PROXY")
        os.environ["HTTPS_PROXY"] = "http://proxy.invalid:3128"
        try:
            self.assertNotIn("HTTPS_PROXY", fetch_part.clean_env())
        finally:
            if saved is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = saved


class ProjectCase(unittest.TestCase):
    """A project directory with the assets a successful fetch would have written."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        # resolve(): the wrapper resolves the STEP path it reports, and macOS's /var ->
        # /private/var symlink would otherwise fail the comparison on every developer laptop.
        self.project = Path(self.tempdir.name).resolve()
        self.lib_dir = self.project / "lib"
        (self.lib_dir / "project.pretty").mkdir(parents=True)
        (self.lib_dir / "project.3dshapes").mkdir(parents=True)
        (self.lib_dir / "project.kicad_sym").write_text(SYMBOL_LIB)
        self.mod = self.lib_dir / "project.pretty" / "C0603.kicad_mod"
        self.mod.write_text(FOOTPRINT)
        (self.lib_dir / "project.3dshapes" / "C0603.step").write_text("ISO-10303-21;\n")

    def tearDown(self):
        self.tempdir.cleanup()

    def jlc_log(self) -> str:
        return JLC_LOG.format(lib=self.lib_dir)


class JlcResultTests(ProjectCase):
    def test_successful_fetch_reports_the_three_assets(self):
        runner = FakeRunner(0, stderr=self.jlc_log())
        res = fetch_part.run_jlc2kicadlib(self.project, "project", "C14663",
                                          exe="JLC2KiCadLib", runner=runner)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["backend"], "jlc2kicad")
        # The logged "CC0603KRX7R9BB104_0" is not the written symbol name.
        self.assertEqual(res["symbol"], "project:CC0603KRX7R9BB104")
        self.assertEqual(res["footprint"], "project:C0603")
        self.assertEqual(res["footprint_file"], str(self.mod))
        self.assertEqual(res["model_step"],
                         str(self.lib_dir / "project.3dshapes" / "C0603.step"))
        self.assertEqual(runner.kwargs[0]["cwd"], str(self.project.resolve()))

    def test_model_binding_is_rewritten_project_relative(self):
        runner = FakeRunner(0, stderr=self.jlc_log())
        res = fetch_part.run_jlc2kicadlib(self.project, "project", "C14663",
                                          runner=runner)

        self.assertEqual(res["model_binding"],
                         "${KIPRJMOD}/lib/project.3dshapes/C0603.step")
        self.assertIn('(model "${KIPRJMOD}/lib/project.3dshapes/C0603.step"',
                      self.mod.read_text())

    def test_rewriting_is_idempotent(self):
        fetch_part.rewrite_model_binding(self.mod, "project")
        once = self.mod.read_text()
        self.assertEqual(fetch_part.rewrite_model_binding(self.mod, "project"),
                         "${KIPRJMOD}/lib/project.3dshapes/C0603.step")
        self.assertEqual(self.mod.read_text(), once)

    def test_unknown_id_fails_despite_exit_code_zero(self):
        runner = FakeRunner(0, stderr=JLC_UNKNOWN_ID_LOG)
        res = fetch_part.run_jlc2kicadlib(self.project, "project", "C99999999",
                                          runner=runner)

        self.assertFalse(res["ok"])
        self.assertNotIn("symbol", res)
        self.assertNotIn("footprint", res)

    def test_missing_footprint_file_fails(self):
        self.mod.unlink()
        runner = FakeRunner(0, stderr=self.jlc_log())
        res = fetch_part.run_jlc2kicadlib(self.project, "project", "C14663",
                                          runner=runner)

        self.assertFalse(res["ok"])

    def test_symbol_absent_from_the_library_fails(self):
        (self.lib_dir / "project.kicad_sym").write_text("(kicad_symbol_lib)\n")
        runner = FakeRunner(0, stderr=self.jlc_log())
        res = fetch_part.run_jlc2kicadlib(self.project, "project", "C14663",
                                          runner=runner)

        self.assertFalse(res["ok"])

    def test_top_level_symbol_names_skips_unit_symbols(self):
        self.assertEqual(fetch_part.top_level_symbol_names(SYMBOL_LIB),
                         ["CC0603KRX7R9BB104"])


class EasyedaResultTests(ProjectCase):
    def test_fallback_backend_result_shape_is_identical(self):
        (self.lib_dir / "project.3dshapes"
         / "C0603_L1.6-W0.8-H0.8.step").write_text("ISO-10303-21;\n")
        runner = FakeRunner(0, stdout=EASYEDA_LOG.format(lib=self.lib_dir))
        res = fetch_part.run_easyeda2kicad(self.project, "project", "C14663",
                                           exe="easyeda2kicad", runner=runner)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["backend"], "easyeda2kicad")
        self.assertEqual(res["symbol"], "project:CC0603KRX7R9BB104")
        self.assertEqual(res["footprint"], "project:C0603")
        self.assertEqual(runner.commands[0][0], "easyeda2kicad")


class MainTests(ProjectCase):
    def invoke(self, argv, runner, installed=("jlc2kicad", "easyeda2kicad")):
        out, err = io.StringIO(), io.StringIO()
        rc = fetch_part.main(argv, runner=runner, find=fake_finder(*installed),
                             out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_default_run_uses_jlc2kicadlib_and_registers_the_libraries(self):
        runner = FakeRunner(0, stderr=self.jlc_log())
        rc, out, err = self.invoke(
            ["--project", str(self.project), "--lcsc", "C14663"], runner)

        self.assertEqual(rc, 0, err)
        report = json.loads(out)
        self.assertEqual(report["backend"], "jlc2kicad")
        self.assertTrue(report["ok"])
        self.assertEqual(runner.commands[0][0], "/fake/venv/bin/JLC2KiCadLib")
        self.assertNotIn("AGPL", err)
        self.assertIn('(name "project")', (self.project / "sym-lib-table").read_text())
        self.assertIn("${KIPRJMOD}/lib/project.pretty",
                      (self.project / "fp-lib-table").read_text())

    def test_fallback_prints_the_licence_warning_once(self):
        (self.lib_dir / "project.3dshapes"
         / "C0603_L1.6-W0.8-H0.8.step").write_text("ISO-10303-21;\n")
        runner = FakeRunner(0, stdout=EASYEDA_LOG.format(lib=self.lib_dir))
        rc, out, err = self.invoke(
            ["--project", str(self.project), "--lcsc", "C14663"], runner,
            installed=("easyeda2kicad",))

        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["backend"], "easyeda2kicad")
        self.assertEqual(err.count("AGPL-3.0"), 1)
        self.assertEqual(runner.commands[0][0], "/fake/venv/bin/easyeda2kicad")

    def test_no_backend_installed_exits_one_without_running_anything(self):
        runner = FakeRunner(0)
        rc, out, err = self.invoke(
            ["--project", str(self.project), "--lcsc", "C14663"], runner,
            installed=())

        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(runner.commands, [])

    def test_ledger_rows_are_promoted_with_the_fetched_assets(self):
        ledger = self.project / "parts.json"
        ledger.write_text(json.dumps({"project": "t", "parts": [
            {"ref_group": ["C1"], "value": "100nF", "lcsc": "C14663",
             "status": "approved"}]}))
        runner = FakeRunner(0, stderr=self.jlc_log())
        rc, _out, err = self.invoke(
            ["--project", str(self.project), "--parts", str(ledger)], runner)

        self.assertEqual(rc, 0, err)
        row = json.loads(ledger.read_text())["parts"][0]
        self.assertEqual(row["status"], "fetched")
        self.assertEqual(row["symbol"], "project:CC0603KRX7R9BB104")
        self.assertEqual(row["footprint"], "project:C0603")
        self.assertIn("fetched_at", row)

    def test_failed_fetch_exits_one(self):
        runner = FakeRunner(0, stderr=JLC_UNKNOWN_ID_LOG)
        rc, out, _err = self.invoke(
            ["--project", str(self.project), "--lcsc", "C99999999"], runner)

        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])


if __name__ == "__main__":
    unittest.main()
