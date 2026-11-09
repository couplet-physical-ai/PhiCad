#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Copyright (C) 2026 Exowatt, Inc. / KiCad Developers.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.

"""`phicad doctor` must notice that a built binary cannot actually run.

The bug this pins down, found while collecting release evidence: `brew upgrade` moved protobuf
36.0.0 -> 36.1.0, both built binaries kept linking the old dylib, and every invocation died at
dyld with "Library not loaded: libprotobuf.36.0.0.dylib". The GUI would not start and the Agent
View rendered a stack trace where the board should have been.

`phicad doctor` reported a healthy machine throughout, because it checked that the protobuf
FORMULA was installed -- and it was, at the new version. Formula-installed and binary-loadable
are different properties, and only the second one matters to somebody trying to use the app.

Two traps worth keeping in the tests, both of which bit during implementation:

  * `set -euo pipefail` is on, so capturing a failing command as `out=$(cmd)` aborts doctor
    mid-report. The first version exited 133 and printed nothing after the formula list.
    A diagnostic that dies on the failure it is meant to diagnose is worse than none.
  * The GUI does NOT answer `--version` on the command line -- it opens a window and waits
    (measured: exit 124 under `timeout 30`). So the GUI must be checked WITHOUT executing it,
    or doctor hangs and hijacks the user's screen.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

PHICAD = pathlib.Path(__file__).resolve().parent / "phicad"


def run_doctor(build_dir: pathlib.Path) -> tuple[int, str]:
    """`phicad doctor` against a synthetic build tree; stdout+stderr merged.

    Merged because the warning goes to stderr while the `fix:` hint goes to stdout -- a split
    this script uses throughout, and a real user sees both together in a terminal.
    """
    env = dict(os.environ, PHICAD_BUILD=str(build_dir))
    env.pop("HTTPS_PROXY", None)
    env.pop("https_proxy", None)
    proc = subprocess.run([str(PHICAD), "doctor"], capture_output=True, text=True,
                          env=env, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def make_tree(root: pathlib.Path, *, cli: str | None, gui: bool = False) -> pathlib.Path:
    """A build tree whose kicad-cli behaves as `cli` dictates.

    cli=None      -> no binary at all (a tree that has not been built)
    cli="ok"      -> prints a version and exits 0
    cli="broken"  -> reproduces the real dyld failure verbatim, exit 133
    cli="silent"  -> aborts without a loader message, like a broken macOS login session
    """
    macos = root / "kicad" / "PhiCad.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (root / "build.ninja").write_text("# synthetic\n")

    if cli == "ok":
        p = macos / "kicad-cli"
        p.write_text("#!/bin/sh\necho 10.99.0\n")
        p.chmod(0o755)
    elif cli == "broken":
        p = macos / "kicad-cli"
        p.write_text(
            "#!/bin/sh\n"
            "echo \"dyld[1]: Library not loaded: "
            "/opt/homebrew/opt/protobuf/lib/libprotobuf.36.0.0.dylib\" >&2\n"
            "echo \"  Reason: tried: (no such file)\" >&2\n"
            "exit 133\n")
        p.chmod(0o755)
    elif cli == "silent":
        p = macos / "kicad-cli"
        p.write_text("#!/bin/sh\nexit 134\n")
        p.chmod(0o755)

    if gui:
        g = macos / "PhiCad"
        g.write_text("#!/bin/sh\nsleep 600\n")     # never answers --version, like the real one
        g.chmod(0o755)
    return root


class DoctorReportsRunnability(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_broken_binary_is_reported(self):
        """The regression: a built kicad-cli that dies at dyld must be called out."""
        rc, out = run_doctor(make_tree(self.root, cli="broken"))
        self.assertIn("BUILT BUT WILL NOT RUN", out,
                     "doctor did not notice that kicad-cli cannot run")

    def test_the_missing_library_is_named(self):
        """Naming the library IS the diagnosis -- without it the user has to guess."""
        _, out = run_doctor(make_tree(self.root, cli="broken"))
        self.assertIn("libprotobuf.36.0.0.dylib", out)

    def test_the_fix_is_offered(self):
        _, out = run_doctor(make_tree(self.root, cli="broken"))
        self.assertIn("phicad configure", out)
        self.assertIn("brew upgrade", out, "the usual cause should be named")

    def test_doctor_still_exits_zero_on_a_broken_tree(self):
        """The `set -e` trap: doctor must REPORT the failure, not die of it.

        The first implementation exited 133 and truncated its own report.
        """
        rc, out = run_doctor(make_tree(self.root, cli="broken"))
        self.assertEqual(rc, 0, f"doctor aborted instead of reporting (exit {rc})")
        self.assertIn("clang", out, "the report was truncated before the end")

    def test_a_silent_early_crash_is_reported_without_aborting_doctor(self):
        """A non-dyld crash must not trip pipefail in the diagnostic itself."""
        rc, out = run_doctor(make_tree(self.root, cli="silent"))
        self.assertEqual(rc, 0, f"doctor aborted on a silent child failure (exit {rc})")
        self.assertIn("BUILT BUT WILL NOT RUN", out)
        self.assertIn("status 134", out)
        self.assertIn("logged-in desktop shell", out)

    def test_a_working_binary_is_reported_as_running(self):
        rc, out = run_doctor(make_tree(self.root, cli="ok"))
        self.assertEqual(rc, 0)
        self.assertIn("runs, reports 10.99.0", out)
        self.assertNotIn("BUILT BUT WILL NOT RUN", out, "false alarm on a healthy tree")

    def test_an_unbuilt_tree_is_not_an_error(self):
        """Not having built yet is a normal state, reported as guidance."""
        rc, out = run_doctor(make_tree(self.root, cli=None))
        self.assertEqual(rc, 0)
        self.assertIn("not built yet", out)
        self.assertNotIn("BUILT BUT WILL NOT RUN", out)

    def test_the_gui_is_never_executed(self):
        """A GUI that never answers --version must not hang doctor.

        The fixture GUI sleeps for 600 s. If doctor executed it, this test would time out
        rather than fail -- which is itself the signal.
        """
        rc, out = run_doctor(make_tree(self.root, cli="ok", gui=True))
        self.assertEqual(rc, 0)
        self.assertIn("PhiCad (GUI)", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
