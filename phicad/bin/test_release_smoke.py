#!/usr/bin/env python3
"""Regression tests for the clean-prefix macOS release smoke."""

from __future__ import annotations

import importlib.util
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "phicad"
HELPER = HERE / "release_smoke.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("phicad_release_smoke", HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {HELPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BundleFixture:
    def __init__(self, root: Path) -> None:
        self.app = root / "PhiCad.app"
        for relative in (
            "Contents/MacOS",
            "Contents/Frameworks",
            "Contents/PlugIns",
            "Contents/SharedSupport/plugins/ai-view",
        ):
            (self.app / relative).mkdir(parents=True, exist_ok=True)
        with (self.app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleExecutable": "PhiCad"}, stream)
        for relative in ("Contents/PlugIns/_pcbnew.kiface", "Contents/PlugIns/_eeschema.kiface"):
            (self.app / relative).write_text("fixture\n", encoding="utf-8")
        (self.app / "Contents/SharedSupport/plugins/ai-view/plugin.json").write_text(
            "{}\n", encoding="utf-8"
        )
        self.make_executable("Contents/MacOS/PhiCad", "#!/bin/sh\nexit 0\n")
        self.make_executable("Contents/MacOS/kicad-cli", "#!/bin/sh\nprintf '10.99.0\\n'\n")

    def make_executable(self, relative: str, body: str) -> None:
        path = self.app / relative
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


class BundleValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_helper()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = BundleFixture(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_launch_scrubs_build_tree_environment(self) -> None:
        capture = self.root / "environment.txt"
        cli = """#!/bin/sh
printf '%s|%s|%s|%s|%s|%s\\n' \\
  "${KICAD_RUN_FROM_BUILD_DIR-unset}" "${KICAD_BUILD_PATHS-unset}" \\
  "${KICAD_STOCK_DATA_HOME-unset}" "${DYLD_LIBRARY_PATH-unset}" \\
  "${PHICAD_BUILD-unset}" "${KEEP_ME-unset}" > "$CAPTURE"
printf '10.99.0\\n'
"""
        self.fixture.make_executable("Contents/MacOS/kicad-cli", cli)
        env = {
            "CAPTURE": str(capture),
            "KICAD_RUN_FROM_BUILD_DIR": "1",
            "KICAD_BUILD_PATHS": "/build",
            "KICAD_STOCK_DATA_HOME": "/source",
            "DYLD_LIBRARY_PATH": "/build/lib",
            "PHICAD_BUILD": "/build",
            "KEEP_ME": "present",
        }
        result = self.mod.smoke_bundle(self.fixture.app, platform="linux", launch_env=env)
        self.assertEqual(result["version"], "10.99.0")
        self.assertEqual(capture.read_text(encoding="utf-8").strip(), "unset|unset|unset|unset|unset|present")

    def test_missing_fork_plugin_fails(self) -> None:
        (self.fixture.app / "Contents/SharedSupport/plugins/ai-view/plugin.json").unlink()
        with self.assertRaisesRegex(self.mod.SmokeError, "ai-view/plugin.json"):
            self.mod.smoke_bundle(self.fixture.app, platform="linux")

    def test_build_tree_shared_support_symlink_fails(self) -> None:
        shared = self.fixture.app / "Contents/SharedSupport"
        shutil.rmtree(shared)
        shared.symlink_to(self.root)
        with self.assertRaisesRegex(self.mod.SmokeError, "build-tree symlink"):
            self.mod.smoke_bundle(self.fixture.app, platform="linux")

    def test_wrong_bundle_executable_fails(self) -> None:
        with (self.fixture.app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleExecutable": "kicad"}, stream)
        with self.assertRaisesRegex(self.mod.SmokeError, "expected 'PhiCad'"):
            self.mod.smoke_bundle(self.fixture.app, platform="linux")

    def test_failed_cli_launch_fails(self) -> None:
        self.fixture.make_executable("Contents/MacOS/kicad-cli", "#!/bin/sh\necho broken >&2\nexit 7\n")
        with self.assertRaisesRegex(self.mod.SmokeError, r"failed \(7\): broken"):
            self.mod.smoke_bundle(self.fixture.app, platform="linux")

    def test_macos_signature_is_checked_before_launch(self) -> None:
        commands = []

        def fake_runner(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "10.99.0\n", "")

        self.mod.smoke_bundle(self.fixture.app, platform="darwin", runner=fake_runner)
        self.assertEqual(commands[0][0], "/usr/bin/codesign")
        self.assertEqual(commands[1][-1], "--version")


class InstallCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "phicad/bin").mkdir(parents=True)
        shutil.copy2(WRAPPER, self.root / "phicad/bin/phicad")
        shutil.copy2(HELPER, self.root / "phicad/bin/release_smoke.py")
        self.build = self.root / "build/mac-release"
        self.prefix = self.root / "build/install/mac-release"
        self.build.mkdir(parents=True)
        (self.build / "build.ninja").write_text("# fixture\n", encoding="utf-8")
        (self.build / "CMakeCache.txt").write_text(
            "CMAKE_BUILD_TYPE:STRING=Release\n"
            f"CMAKE_INSTALL_PREFIX:PATH={self.prefix}\n"
            "KICAD_BUILD_QA_TESTS:BOOL=OFF\n",
            encoding="utf-8",
        )
        self.ngspice_prefix = self.root / "homebrew/opt/libngspice"
        ngspice_lib = self.ngspice_prefix / "lib"
        (ngspice_lib / "ngspice").mkdir(parents=True)
        (ngspice_lib / "libngspice.0.dylib").write_bytes(b"fixture ngspice library\n")
        (ngspice_lib / "libunrelated.dylib").write_bytes(b"must not be staged\n")
        (ngspice_lib / "ngspice/analog.cm").write_bytes(b"fixture codemodel\n")

        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self._write_fake("uname", "#!/bin/sh\nprintf 'Darwin\\n'\n")
        self._write_fake(
            "brew",
            f"#!/bin/sh\n[ \"$1\" = --prefix ]\nprintf '%s\\n' '{self.ngspice_prefix}'\n",
        )
        self._write_fake("cmake", self._fake_cmake())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_fake(self, name: str, body: str) -> None:
        path = self.fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def _fake_cmake(self) -> str:
        return f"""#!/bin/sh
set -eu
[ "$1" = --install ]
[ "$2" = "{self.build}" ]
[ ! -e "{self.prefix}/stale" ] || exit 90
[ ! -L "{self.build}/kicad/PhiCad.app/Contents/SharedSupport" ] || exit 91
app="{self.prefix}/PhiCad.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Frameworks" "$app/Contents/PlugIns" \\
  "$app/Contents/SharedSupport/plugins/ai-view"
printf '<plist><dict><key>CFBundleExecutable</key><string>PhiCad</string></dict></plist>\\n' \\
  > "$app/Contents/Info.plist"
printf '#!/bin/sh\\nexit 0\\n' > "$app/Contents/MacOS/PhiCad"
printf '#!/bin/sh\\nprintf "10.99.0\\\\n"\\n' > "$app/Contents/MacOS/kicad-cli"
chmod +x "$app/Contents/MacOS/PhiCad" "$app/Contents/MacOS/kicad-cli"
printf fixture > "$app/Contents/PlugIns/_pcbnew.kiface"
printf fixture > "$app/Contents/PlugIns/_eeschema.kiface"
printf '{{}}\\n' > "$app/Contents/SharedSupport/plugins/ai-view/plugin.json"
if [ -x /usr/bin/codesign ]; then /usr/bin/codesign --force --deep --sign - "$app" >/dev/null 2>&1; fi
"""

    def _run(self):
        env = os.environ.copy()
        env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        env["PHICAD_BUILD"] = str(self.build)
        return subprocess.run(
            [str(self.root / "phicad/bin/phicad"), "install"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_install_cleans_prefix_stages_and_smokes(self) -> None:
        self.prefix.mkdir(parents=True)
        (self.prefix / "stale").write_text("must be removed\n", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("release smoke passed", result.stdout)
        self.assertFalse((self.prefix / "stale").exists())

    def test_install_removes_the_owned_build_tree_shared_support_link(self) -> None:
        shared = self.build / "kicad/PhiCad.app/Contents/SharedSupport"
        shared.parent.mkdir(parents=True)
        shared.symlink_to("../../..")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(shared.exists())
        self.assertFalse(shared.is_symlink())

    def test_install_refuses_an_unexpected_build_tree_shared_support_link(self) -> None:
        shared = self.build / "kicad/PhiCad.app/Contents/SharedSupport"
        shared.parent.mkdir(parents=True)
        shared.symlink_to("unexpected")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unexpected build SharedSupport link", result.stderr)
        self.assertTrue(shared.is_symlink())

    def test_install_refuses_non_release_cache(self) -> None:
        cache = self.build / "CMakeCache.txt"
        cache.write_text(cache.read_text(encoding="utf-8").replace("Release", "RelWithDebInfo"), encoding="utf-8")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a Release build", result.stderr)

    def test_install_refuses_unexpected_prefix(self) -> None:
        cache = self.build / "CMakeCache.txt"
        cache.write_text(
            cache.read_text(encoding="utf-8").replace(str(self.prefix), "/usr/local"), encoding="utf-8"
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe install prefix", result.stderr)

    def test_configure_release_pins_disposable_install_prefix(self) -> None:
        args_log = self.root / "cmake-args.txt"
        self._write_fake(
            "cmake",
            f"""#!/bin/sh
printf '%s\\n' "$@" > "{args_log}"
mkdir -p "{self.build}"
touch "{self.build}/compile_commands.json"
""",
        )
        env = os.environ.copy()
        env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        env["PHICAD_BUILD"] = str(self.build)
        result = subprocess.run(
            [str(self.root / "phicad/bin/phicad"), "configure", "release"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        args = args_log.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"-DCMAKE_INSTALL_PREFIX={self.prefix}", args)
        staged = self.build / "phicad-release-deps/ngspice"
        staged_library = staged / "libngspice.0.dylib"
        self.assertIn(f"-DNGSPICE_LIBRARY={staged_library}", args)
        self.assertTrue(staged_library.is_file())
        self.assertFalse(staged_library.is_symlink())
        self.assertEqual(staged_library.read_bytes(), b"fixture ngspice library\n")
        self.assertFalse((staged / "libunrelated.dylib").exists())
        self.assertTrue((staged / "ngspice").is_dir())
        self.assertFalse((staged / "ngspice").is_symlink())
        self.assertEqual((staged / "ngspice/analog.cm").read_bytes(), b"fixture codemodel\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
