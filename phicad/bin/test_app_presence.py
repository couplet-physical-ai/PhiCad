#!/usr/bin/env python3
"""Regression tests for build-artifact presence reporting in the `phicad` wrapper.

Both behaviours here were real defects, and they had the same root cause: a *directory* was
treated as proof that an *application* existed.

`build/mac-dev/kicad/PhiCad.app/` and its editor plug-ins appear as soon as the tree is
configured, but the GUI executable is only written when the `kicad` target links.  So

  * `phicad open` passed a `[[ -d ]]` guard and handed an executable-less bundle to `open -a`,
    which fails -- and `open -a` exits 0, so the failure looked like a successful launch; and
  * `phicad doctor` reported a healthy machine on a tree whose window could never open, because
    it only ever checked Homebrew formulae.

The fix checks the executable and reports each artifact separately.  These tests pin that, so a
tree that cannot open a window can never again describe itself as ready.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "phicad"


def build_section(text: str) -> str:
    """Only the build-state block.

    `doctor` also reports Homebrew formulae, and on a Linux CI runner every one of those is
    legitimately MISSING. Asserting over the whole output conflates "this machine has no
    Homebrew" with "this tree has no GUI", which are unrelated failures with different fixes.
    """
    marker = "build tree "
    index = text.find(marker)
    return text[index:] if index >= 0 else ""


def run(args, build: Path, timeout: int = 240) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PHICAD_BUILD"] = str(build)
    # Keep the child from inheriting a socket that points at a live editor.
    env.pop("KICAD_API_SOCKET", None)
    return subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def make_tree(root: Path, *, gui: bool, cli: bool = True, kifaces: int = 6) -> Path:
    """A build tree in a chosen state of completeness."""
    build = root / "mac-dev"
    macos = build / "kicad/PhiCad.app/Contents/MacOS"
    plugins = build / "kicad/PhiCad.app/Contents/PlugIns"
    macos.mkdir(parents=True, exist_ok=True)
    plugins.mkdir(parents=True, exist_ok=True)
    for name, present in (("PhiCad", gui), ("kicad-cli", cli)):
        if present:
            target = macos / name
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
    for index in range(kifaces):
        (plugins / f"_fixture{index}.kiface").write_text("fixture\n", encoding="utf-8")
    return build


class OpenGuardTests(unittest.TestCase):
    def test_open_refuses_a_bundle_with_no_executable(self):
        # The exact defect: the directory exists, so the old `[[ -d ]]` guard passed.
        with tempfile.TemporaryDirectory() as tmp:
            build = make_tree(Path(tmp), gui=False)
            self.assertTrue((build / "kicad/PhiCad.app").is_dir(), "fixture must have the bundle dir")
            result = run(["open"], build)
            self.assertNotEqual(result.returncode, 0, "an unlaunchable app must not report success")
            combined = result.stdout + result.stderr
            self.assertIn("no GUI executable", combined)
            # The message has to name the command that fixes it, not just the failure.
            self.assertIn("phicad build kicad", combined)

    def test_open_names_the_missing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = make_tree(Path(tmp), gui=False)
            result = run(["open"], build)
            self.assertIn("Contents/MacOS/PhiCad", result.stdout + result.stderr)

    def test_open_reports_an_absent_bundle_differently(self):
        # A tree that was never configured is a different problem from one that was configured
        # but never linked, and it gets a different fix.
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "never-configured"
            result = run(["open"], build)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no app at", result.stdout + result.stderr)


class DoctorReportTests(unittest.TestCase):
    def test_doctor_reports_a_missing_gui_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = make_tree(Path(tmp), gui=False, kifaces=6)
            out = build_section(run(["doctor"], build).stdout)
            self.assertIn("GUI app", out)
            self.assertIn("MISSING", out)
            self.assertIn("phicad build kicad", out)

    def test_doctor_reports_a_present_gui_as_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = make_tree(Path(tmp), gui=True, kifaces=6)
            out = build_section(run(["doctor"], build).stdout)
            self.assertIn("GUI app", out)
            # Scoped to the build block on purpose: a Linux runner with no Homebrew reports
            # MISSING formulae above this, and that says nothing about the GUI.
            self.assertNotIn("MISSING", out)
            self.assertIn("6/6", out)

    def test_doctor_counts_kifaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = make_tree(Path(tmp), gui=True, kifaces=3)
            out = build_section(run(["doctor"], build).stdout)
            self.assertIn("3/6", out)
            self.assertIn("some editor plug-ins are missing", out)

    def test_doctor_flags_an_unconfigured_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run(["doctor"], Path(tmp) / "absent")
            # `warn` writes to stderr (it is a diagnostic) while the fix hint is on stdout, which
            # matches how the existing missing-formulae branch behaves. Assert over both streams
            # rather than pinning the split -- the requirement is that the user sees both.
            combined = result.stdout + result.stderr
            self.assertIn("not configured", combined)
            self.assertIn("phicad configure", combined)

    def test_doctor_separates_cli_from_gui(self):
        # They are produced by different targets and fail independently, so one being present
        # must never imply the other.
        with tempfile.TemporaryDirectory() as tmp:
            build = make_tree(Path(tmp), gui=True, cli=False)
            out = build_section(run(["doctor"], build).stdout)
            self.assertRegex(out, r"kicad-cli\s+MISSING")
            self.assertRegex(out, r"GUI app\s+ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
