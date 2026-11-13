#!/usr/bin/env python3
"""Tests for the gate scripts behind `phicad check`.

The gates decide whether every other change is allowed to merge, so their own failure modes
matter more than most code's.  What is pinned here is the **exit-code contract** they share
with `run_gate` in `phicad/bin/phicad`:

    0 = looked, clean        1 = looked, found problems        2 = could not look

The third case is the one that bites.  `check_style.py` reads its file list from `git
ls-files` (the only way to skip build output without a second ignore list), so running it on
an extracted archive or a copied tree used to raise `CalledProcessError` -- a traceback that
reads like a bug in the checker, counted as a style violation, sending the reader hunting a
problem that does not exist.  `run_gate` reports "I could not look" as a broken gate, so it
cannot render as either a pass or a normal assertion failure.  `skip_gate` remains the path
for a genuinely missing prerequisite.

Run: python3 phicad/bin/test_check_gates.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent
REPO = BIN.parents[1]


def run(script: str, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(cwd / "phicad" / "bin" / script), *args],
                          capture_output=True, text=True, cwd=str(cwd))


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True)


class StyleGateContractTest(unittest.TestCase):
    def test_clean_repo_exits_zero(self) -> None:
        proc = run("check_style.py", REPO)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("style ok", proc.stdout)

    def test_missing_git_checkout_exits_two_without_a_traceback(self) -> None:
        """A copied tree is a real case; it must explain itself, not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "copy"
            (dest / "phicad" / "bin").mkdir(parents=True)
            for name in ("check_style.py", "check_schemas.py"):
                shutil.copy2(BIN / name, dest / "phicad" / "bin" / name)

            proc = run("check_style.py", dest)
            self.assertEqual(proc.returncode, 2,
                             f"expected 2 (could not look), got {proc.returncode}")
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            combined = proc.stdout + proc.stderr
            self.assertIn("not a git repository", combined)
            self.assertIn("git checkout", combined)

    def test_a_real_violation_still_fails(self) -> None:
        """The point of the exit-2 path is that it did not soften exit 1."""
        probe = REPO / "phicad" / "bin" / "_gate_contract_probe.py"
        probe.write_text("x = 1\ny = 2  \n")            # trailing whitespace
        try:
            subprocess.run(["git", "-C", str(REPO), "add", str(probe)],
                           capture_output=True, check=True)
            proc = run("check_style.py", REPO)
            self.assertEqual(proc.returncode, 1,
                             f"a seeded violation must exit 1, got {proc.returncode}")
            self.assertIn("trailing whitespace", proc.stdout)
        finally:
            subprocess.run(["git", "-C", str(REPO), "rm", "-q", "--cached", str(probe)],
                           capture_output=True)
            probe.unlink(missing_ok=True)


class CheckRunnerContractTest(unittest.TestCase):
    def test_a_gate_that_cannot_run_is_broken_and_fails_check(self) -> None:
        """Exit 2 means the checked thing could not start, not that an optional tool is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "copy"
            shutil.copytree(REPO / "phicad", root / "phicad",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            # Do not recurse into this test while the copied check runs its self-test gate.
            (root / "phicad/bin/test_check_gates.py").write_text("raise SystemExit(0)\n")
            tool = root / "phicad/bin/phicad-tool"
            tool.write_text(tool.read_text() + "\nif (\n")

            git(root, "init", "--quiet")
            git(root, "add", "phicad")
            proc = subprocess.run(
                [str(root / "phicad/bin/phicad"), "check", "fast"],
                capture_output=True, text=True, cwd=str(root),
            )

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 1, combined)
            self.assertIn("BROKEN  shell syntax (phicad-tool) — could not run", combined)
            self.assertNotIn("SKIP  shell syntax (phicad-tool)", combined)
            # Assert the contract, not the whole tally: the pass/skip counts legitimately
            # differ between a dev machine and CI (ruff is installed there, so nothing
            # skips), and pinning them makes this test fail for reasons it does not test.
            #
            # The subject is the mutated phicad-tool specifically. Other gates may also go
            # BROKEN here, because this scratch repo contains only `phicad/` -- a gate that
            # inspects the wider tree legitimately cannot run in it. Requiring exactly one
            # broken gate would make this test depend on the runner's environment, so assert
            # that the summary counts the mutation as broken and nothing as failed instead.
            # This scratch repo holds only `phicad/`, so other gates may report BROKEN or
            # even FAIL here for want of the wider tree -- and which ones do depends on the
            # machine. Assert only what this test is about: a gate that exits 2 is counted
            # as broken, and that alone is enough to make the check exit non-zero.
            broken = [ln for ln in combined.splitlines() if ln.startswith("BROKEN")]
            summary = [ln for ln in combined.splitlines() if " passed, " in ln]
            self.assertTrue(
                any("shell syntax (phicad-tool)" in ln for ln in broken),
                f"expected the mutated tool to be BROKEN.\nbroken={broken}\nsummary={summary}")
            self.assertRegex(combined, r"\b[1-9]\d* broken\b",
                             f"summary did not count a broken gate: {summary}")


class ForkBoundaryContractTest(unittest.TestCase):
    def _repo(self, root: str, extra: dict[str, str] | None = None) -> tuple[Path, str]:
        repo = Path(root) / "repo"
        (repo / "phicad" / "bin").mkdir(parents=True)
        shutil.copy2(BIN / "check_fork_boundary.py", repo / "phicad" / "bin")
        (repo / "phicad" / "baseline.txt").write_text("baseline\n")
        for relative, content in (extra or {}).items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        git(repo, "init", "--quiet")
        git(repo, "config", "user.name", "PhiCad boundary test")
        git(repo, "config", "user.email", "boundary-test@example.invalid")
        git(repo, "config", "commit.gpgsign", "false")
        git(repo, "add", ".")
        git(repo, "commit", "--quiet", "-m", "baseline")
        return repo, git(repo, "rev-parse", "HEAD").stdout.strip()

    def _commit(self, repo: Path) -> None:
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "candidate")

    def test_allows_fork_layer_and_narrow_integration_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp)
            allowed = {
                "phicad/owned.py": "value = 1\n",
                "AGENTS.md": "fork instructions\n",
                "pyproject.toml": "[tool.ruff]\n",
                ".github/workflows/phicad-check.yml": "name: boundary\n",
            }
            for relative, content in allowed.items():
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            self._commit(repo)

            proc = run("check_fork_boundary.py", repo, "--base", base, "--committed-only")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("4 changed path(s)", proc.stdout)

    def test_rejects_an_upstream_path_addition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp)
            path = repo / "pcbnew" / "accidental.txt"
            path.parent.mkdir()
            path.write_text("fork-only content\n")
            self._commit(repo)

            proc = run("check_fork_boundary.py", repo, "--base", base, "--committed-only")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("pcbnew/accidental.txt", proc.stdout)

    def test_rejects_the_forbidden_source_of_a_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp, {"pcbnew/upstream.txt": "upstream\n"})
            (repo / "phicad").mkdir(exist_ok=True)
            git(repo, "mv", "pcbnew/upstream.txt", "phicad/moved.txt")
            self._commit(repo)

            proc = run("check_fork_boundary.py", repo, "--base", base, "--committed-only")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("pcbnew/upstream.txt", proc.stdout)

    def test_rejects_an_untracked_upstream_path_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp)
            path = repo / "cmake" / "accidental.txt"
            path.parent.mkdir()
            path.write_text("fork-only content\n")

            proc = run("check_fork_boundary.py", repo, "--base", base)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("cmake/accidental.txt", proc.stdout)

    def test_missing_base_exits_two_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo(tmp)
            proc = run("check_fork_boundary.py", repo, "--base", "does-not-exist")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("could not inspect", proc.stdout)
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    # --- content-pinned exceptions -------------------------------------------------------
    # An exception admits exactly one blob at one upstream path.  The three things that must
    # hold: that content passes (committed and in the working tree), any other content at the
    # path is still forbidden, and a list that cannot be trusted cannot open the gate.

    FIX = "// upstream file carrying the one authorized fork fix\n"

    def _exceptions(self, repo: Path, entries: list) -> None:
        (repo / "phicad" / "fork-boundary-exceptions.json").write_text(
            json.dumps({"exceptions": entries}, indent=2) + "\n")

    def _entry(self, path: str, blob: str, **overrides) -> dict:
        entry = {"path": path, "blob": blob, "pr": 121, "reason": "tested fix",
                 "authorized_by": "owner, 2026-09-22, verbatim: allow this one fix"}
        entry.update(overrides)
        return entry

    def _blob_of(self, content: str) -> str:
        return subprocess.run(["git", "hash-object", "--stdin"], input=content, text=True,
                              capture_output=True, check=True).stdout.strip()

    def test_a_pinned_exception_admits_exactly_that_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp)
            (repo / "kicad").mkdir()
            (repo / "kicad" / "kicad_cli.cpp").write_text(self.FIX)
            self._exceptions(repo, [self._entry("kicad/kicad_cli.cpp", self._blob_of(self.FIX))])

            local = run("check_fork_boundary.py", repo, "--base", base)   # uncommitted
            self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
            self.assertIn("exception applied: kicad/kicad_cli.cpp", local.stdout)
            self.assertIn("PR #121", local.stdout)

            self._commit(repo)
            ci = run("check_fork_boundary.py", repo, "--base", base, "--committed-only")
            self.assertEqual(ci.returncode, 0, ci.stdout + ci.stderr)
            self.assertIn("1 one-time exception(s) applied", ci.stdout)

    def test_a_pinned_exception_does_not_admit_other_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp)
            (repo / "kicad").mkdir()
            (repo / "kicad" / "kicad_cli.cpp").write_text(self.FIX + "// and one more edit\n")
            self._exceptions(repo, [self._entry("kicad/kicad_cli.cpp", self._blob_of(self.FIX))])
            self._commit(repo)

            proc = run("check_fork_boundary.py", repo, "--base", base, "--committed-only")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("kicad/kicad_cli.cpp", proc.stdout)
            self.assertIn("exactly that content", proc.stdout)
            self.assertNotIn("exception applied", proc.stdout)

    def test_an_exception_does_not_cover_a_second_upstream_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp)
            (repo / "kicad").mkdir()
            (repo / "kicad" / "kicad_cli.cpp").write_text(self.FIX)
            (repo / "kicad" / "other.cpp").write_text("// rode along\n")
            self._exceptions(repo, [self._entry("kicad/kicad_cli.cpp", self._blob_of(self.FIX))])
            self._commit(repo)

            proc = run("check_fork_boundary.py", repo, "--base", base, "--committed-only")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("  kicad/other.cpp", proc.stdout)

    def test_an_untrustworthy_exception_list_cannot_open_the_gate(self) -> None:
        blob = self._blob_of(self.FIX)
        bad_lists = {
            "missing keys": [{"path": "kicad/kicad_cli.cpp", "blob": blob}],
            "allowed path": [self._entry("phicad/owned.py", blob)],
            "short blob": [self._entry("kicad/kicad_cli.cpp", blob[:12])],
            "duplicate": [self._entry("kicad/kicad_cli.cpp", blob)] * 2,
        }
        for label, entries in bad_lists.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                repo, base = self._repo(tmp)
                (repo / "kicad").mkdir()
                (repo / "kicad" / "kicad_cli.cpp").write_text(self.FIX)
                self._exceptions(repo, entries)
                self._commit(repo)

                proc = run("check_fork_boundary.py", repo, "--base", base, "--committed-only")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("could not inspect", proc.stdout)
                self.assertIn("fork-boundary-exceptions.json", proc.stdout)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)


class SchemaGateContractTest(unittest.TestCase):
    def test_clean_repo_exits_zero(self) -> None:
        proc = run("check_schemas.py", REPO)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_reports_a_missing_schema_as_a_problem(self) -> None:
        """Fixed paths, so a missing schema is a real finding, not an environment issue."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "copy"
            (dest / "phicad" / "bin").mkdir(parents=True)
            shutil.copy2(BIN / "check_schemas.py", dest / "phicad" / "bin")
            proc = run("check_schemas.py", dest)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("missing", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
