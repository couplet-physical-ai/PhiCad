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
"""Fetch exact parts (symbol + footprint + 3D model) into a KiCad project library.

Wraps a JLCPCB/LCSC CAD-asset downloader so that an agent can turn a verified
LCSC id ("C14663") into project-local library assets:

    <project>/lib/<libname>.kicad_sym          symbol (appended per part)
    <project>/lib/<libname>.pretty/*.kicad_mod footprint
    <project>/lib/<libname>.3dshapes/*.step    3D model (STEP + WRL)

and registers the library in the project's sym-lib-table / fp-lib-table.
3D bindings are written ${KIPRJMOD}-relative so the project stays portable.

Two backends produce that identical layout:

  jlc2kicad      JLC2KiCadLib (MIT) -- the default, the supported path.
  easyeda2kicad  easyeda2kicad (AGPL-3.0) -- fallback for one release only,
                 used automatically (with a printed warning) when JLC2KiCadLib
                 is not installed.  Scheduled for removal, see parts/README.md.

Usage:
    fetch_part.py --project <dir> --lcsc C14663 C25804 [--lib project]
    fetch_part.py --project <dir> --parts parts.json   # ledger mode (see README)
    fetch_part.py --project <dir> --lcsc C14663 --backend easyeda2kicad

Known upstream quirks this wrapper absorbs:
  - easyeda2kicad (v1.0.1): --project-relative requires an ABSOLUTE --output
    path AND cwd == the project directory, and macOS /tmp is a symlink, so
    everything is Path.resolve()'d before the tool sees it; the output
    directory must already exist.
  - JLC2KiCadLib (v1.3.2): -model_dir is relative to the FOOTPRINT library, so
    the 3D binding it writes into the footprint is
    "$(KIPRJMOD)/../<lib>.3dshapes/x.step" -- relative to the wrong directory.
    The wrapper rewrites it to "${KIPRJMOD}/lib/<lib>.3dshapes/x.step".  It
    also logs the symbol name with a "_0" unit suffix that is not the symbol
    name it writes, and exits 0 on an unknown part id (an "ERROR" log line and
    missing files are the only signal).
  - both: proxy env vars break the EasyEDA/JLCPCB API, so they are stripped
    from the child env.

Exit codes: 0 = all parts fetched, 1 = at least one part failed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "ALL_PROXY", "all_proxy")

LIB_TABLES = {
    "sym-lib-table": ("sym_lib_table", "{lib}.kicad_sym"),
    "fp-lib-table": ("fp_lib_table", "{lib}.pretty"),
}

# backend name -> (executable, PyPI distribution, licence)
BACKENDS = {
    "jlc2kicad": ("JLC2KiCadLib", "JLC2KiCadLib", "MIT"),
    "easyeda2kicad": ("easyeda2kicad", "easyeda2kicad", "AGPL-3.0"),
}
DEFAULT_BACKEND = "jlc2kicad"
FALLBACK_BACKEND = "easyeda2kicad"

# Why the fallback is loud: easyeda2kicad is AGPL-3.0, which is incompatible with
# this fork's GPL-2.0-or-later and with serving PhiCad as a hosted product.  It is
# a subprocess (not linked code), so using it locally is legitimate -- but nobody
# should discover months later that the AGPL tool was silently in the pipeline.
FALLBACK_WARNING = (
    "warning: JLC2KiCadLib (MIT) is not installed -- falling back to"
    " easyeda2kicad, which is AGPL-3.0 and therefore cannot be part of a hosted"
    " PhiCad. This fallback is kept for one release only; install the default"
    " backend with: phicad venv (or pip install JLC2KiCadLib)."
)


def clean_env() -> dict[str, str]:
    import os
    env = dict(os.environ)
    for var in PROXY_VARS:
        env.pop(var, None)
    return env


def find_backend_exe(backend: str, which=shutil.which) -> str | None:
    """Locate a backend executable: this interpreter's venv first, then PATH.

    The venv-local hit matters because `phicad py` runs this script with the
    automation venv's python, and that venv is where `phicad venv` installs the
    backends -- it is not necessarily on PATH.
    """
    exe_name = BACKENDS[backend][0]
    local = Path(sys.executable).with_name(exe_name)
    if local.exists():
        return str(local)
    return which(exe_name)


def choose_backend(requested: str | None, find=find_backend_exe) -> tuple[str | None, str | None, str | None]:
    """Pick the backend to run.  -> (backend, exe, note); note is a warning/error.

    An explicitly requested backend is never silently swapped: a missing one is an
    error.  Only the default (jlc2kicad) falls back to easyeda2kicad, and only
    with FALLBACK_WARNING printed.
    """
    backend = requested or DEFAULT_BACKEND
    exe = find(backend)
    if exe:
        return backend, exe, None

    install = f"pip install {BACKENDS[backend][1]}"
    if requested is not None:
        return None, None, (f"error: backend {backend} requested but"
                            f" {BACKENDS[backend][0]} is not installed --"
                            f" install it with: {install}")

    fallback_exe = find(FALLBACK_BACKEND)
    if fallback_exe:
        return FALLBACK_BACKEND, fallback_exe, FALLBACK_WARNING
    return None, None, ("error: no parts backend installed (looked for"
                        f" {BACKENDS[DEFAULT_BACKEND][0]} and"
                        f" {BACKENDS[FALLBACK_BACKEND][0]}) -- install the default"
                        f" with: {install}, or run: phicad venv")


def top_level_symbol_names(text: str) -> list[str]:
    """Symbol names at library level in a .kicad_sym (2-space indent), in order.

    Nested unit symbols ("NAME_0_1") are indented deeper and are skipped, so the
    list is exactly what a schematic can instantiate as <lib>:<name>.
    """
    return re.findall(r'^  \(symbol "([^"]+)"', text, re.MULTILINE)


def jlc2kicadlib_command(exe: str, project: Path, lib: str, lcsc_id: str) -> list[str]:
    """Argv that makes JLC2KiCadLib write the layout this wrapper promises.

    -symbol_lib_dir is the library root itself (not the default "symbol/"
    subdirectory), -footprint_lib carries the ".pretty" suffix KiCad expects, and
    -model_dir climbs out of the .pretty back into <lib>.3dshapes because
    JLC2KiCadLib resolves it relative to the footprint library.
    """
    lib_dir = (project / "lib").resolve()
    return [exe, lcsc_id,
            "-dir", str(lib_dir),
            "-symbol_lib", lib,
            "-symbol_lib_dir", ".",
            "-footprint_lib", f"{lib}.pretty",
            "-models", "STEP", "WRL",
            "-model_dir", f"../{lib}.3dshapes",
            "-model_base_variable", "KIPRJMOD",
            "-logging_level", "INFO"]


def easyeda2kicad_command(exe: str, project: Path, lib: str, lcsc_id: str) -> list[str]:
    """Argv for the AGPL fallback backend (unchanged behaviour)."""
    output = (project / "lib" / f"{lib}.kicad_sym").resolve()
    return [exe, "--full", "--overwrite", "--project-relative",
            "--lcsc_id", lcsc_id, "--output", str(output)]


def rewrite_model_binding(mod_path: Path, lib: str) -> str | None:
    """Repoint a JLC2KiCadLib 3D binding at the project root.  -> new path or None.

    JLC2KiCadLib writes "$(KIPRJMOD)/../<lib>.3dshapes/x.step" because it treats
    -model_dir as relative to the footprint library; KIPRJMOD is the *project*
    directory, so the stored path has to become "${KIPRJMOD}/lib/<lib>...".
    Idempotent: a binding already in the correct form is returned unchanged.
    """
    if not mod_path.exists():
        return None
    text = mod_path.read_text()
    wanted = "${KIPRJMOD}/lib/" + f"{lib}.3dshapes/"
    fixed = re.sub(r"\$[({]KIPRJMOD[)}]/(?:\.\./)*" + re.escape(f"{lib}.3dshapes/"),
                   wanted.replace("\\", "\\\\"), text)
    if fixed != text:
        mod_path.write_text(fixed)
    m = re.search(r'\(model "([^"]+)"', fixed)
    return m.group(1) if m else None


def run_jlc2kicadlib(project: Path, lib: str, lcsc_id: str, exe: str = "JLC2KiCadLib",
                     runner=subprocess.run) -> dict:
    """Fetch one LCSC id with JLC2KiCadLib (MIT).  Same result dict as the fallback."""
    lib_dir = project / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    cmd = jlc2kicadlib_command(exe, project, lib, lcsc_id)
    proc = runner(cmd, cwd=str(project.resolve()), env=clean_env(),
                  capture_output=True, text=True, timeout=180)
    out = (proc.stdout or "") + (proc.stderr or "")

    result: dict = {"lcsc": lcsc_id, "backend": "jlc2kicad",
                    "ok": proc.returncode == 0, "log": out.strip()}
    if proc.returncode != 0:
        return result
    # The tool exits 0 for an unknown part id and only logs the failure.
    if re.search(r"^.* - ERROR - ", out, re.MULTILINE):
        result["ok"] = False

    m = re.search(r"Creating symbol (\S+) in ", out)
    if m:
        logged = m.group(1)
        # The log adds a unit suffix ("NAME_0") that is not the written name.
        candidates = [re.sub(r"_\d+$", "", logged), logged]
        sym_file = lib_dir / f"{lib}.kicad_sym"
        written = top_level_symbol_names(sym_file.read_text()) if sym_file.exists() else []
        name = next((c for c in candidates if c in written), candidates[0])
        result["symbol"] = f"{lib}:{name}"
        result["ok"] &= name in written

    m = re.search(r"Created '(.+\.kicad_mod)'", out)
    if m:
        mod = Path(m.group(1))
        result["footprint"] = f"{lib}:{mod.stem}"
        result["footprint_file"] = str(mod)
        result["ok"] &= mod.exists()

    m = re.search(r"STEP model created at (\S+)", out)
    if m:
        step = Path(m.group(1).rstrip(".")).resolve()
        result["model_step"] = str(step)
        result["ok"] &= step.exists()

    # The binding JLC2KiCadLib stores is relative to the footprint library, not to
    # the project: fix it in place so audit_parts.py can resolve ${KIPRJMOD}.
    if "footprint_file" in result:
        binding = rewrite_model_binding(Path(result["footprint_file"]), lib)
        if binding:
            result["model_binding"] = binding

    if "symbol" not in result and "footprint" not in result:
        result["ok"] = False
    return result


def run_easyeda2kicad(project: Path, lib: str, lcsc_id: str, exe: str | None = None,
                      runner=subprocess.run) -> dict:
    """Fetch one LCSC id into <project>/lib/<lib>.kicad_sym (+.pretty/.3dshapes).

    The AGPL-3.0 fallback backend, kept for one release; see FALLBACK_WARNING.
    """
    lib_dir = project / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)

    if exe is None:
        local = Path(sys.executable).with_name("easyeda2kicad")
        exe = str(local) if local.exists() else "easyeda2kicad"
    cmd = easyeda2kicad_command(exe, project, lib, lcsc_id)
    proc = runner(cmd, cwd=str(project.resolve()), env=clean_env(),
                  capture_output=True, text=True, timeout=180)
    out = (proc.stdout or "") + (proc.stderr or "")

    result: dict = {"lcsc": lcsc_id, "backend": "easyeda2kicad",
                    "ok": proc.returncode == 0, "log": out.strip()}
    if proc.returncode != 0:
        return result

    m = re.search(r"Created Kicad symbol for ID : \S+\s+Symbol name : (\S+)", out)
    if m:
        result["symbol"] = f"{lib}:{m.group(1)}"
    m = re.search(r"Footprint name: (\S+)", out)
    if m:
        result["footprint"] = f"{lib}:{m.group(1)}"
        mod = lib_dir / f"{lib}.pretty" / f"{m.group(1)}.kicad_mod"
        result["footprint_file"] = str(mod)
        result["ok"] &= mod.exists()
    m = re.search(r"3D model path \(step\): (\S+)", out)
    if m:
        step = Path(m.group(1))
        result["model_step"] = str(step)
        result["ok"] &= step.exists()
    # A fetch that created neither a footprint nor a symbol is a failure even
    # if the tool exited 0 (e.g. bogus id that resolves to an empty component).
    if "symbol" not in result and "footprint" not in result:
        result["ok"] = False
    return result


def run_backend(project: Path, lib: str, lcsc_id: str, backend: str, exe: str,
                runner=subprocess.run) -> dict:
    """Dispatch one fetch to the chosen backend.  -> the common result dict."""
    if backend == "jlc2kicad":
        return run_jlc2kicadlib(project, lib, lcsc_id, exe=exe, runner=runner)
    if backend == "easyeda2kicad":
        return run_easyeda2kicad(project, lib, lcsc_id, exe=exe, runner=runner)
    raise ValueError(f"unknown backend: {backend}")


def ensure_lib_table(project: Path, lib: str) -> list[str]:
    """Register lib in the project's sym-lib-table / fp-lib-table (idempotent)."""
    touched = []
    for fname, (root_tok, uri_tpl) in LIB_TABLES.items():
        path = project / fname
        uri = "${KIPRJMOD}/lib/" + uri_tpl.format(lib=lib)
        entry = (f'  (lib (name "{lib}")(type "KiCad")(uri "{uri}")'
                 f'(options "")(descr "phicad parts pipeline"))')
        if path.exists():
            text = path.read_text()
            if f'(name "{lib}")' in text:
                continue
            idx = text.rstrip().rfind(")")
            text = text.rstrip()[:idx] + entry + "\n)\n"
        else:
            text = f"({root_tok}\n  (version 7)\n{entry}\n)\n"
        path.write_text(text)
        touched.append(fname)
    return touched


def load_ledger(path: Path) -> dict:
    data = json.loads(path.read_text())
    if "parts" not in data or not isinstance(data["parts"], list):
        sys.exit(f"error: {path} has no 'parts' array (see phicad/parts/README.md)")
    return data


def main(argv: list[str] | None = None, *, runner=subprocess.run,
         find=find_backend_exe, out=sys.stdout, err=sys.stderr) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True, type=Path,
                    help="KiCad project directory (contains the .kicad_pro)")
    ap.add_argument("--lcsc", nargs="*", default=[], help="LCSC ids, e.g. C14663")
    ap.add_argument("--parts", type=Path,
                    help="parts.json ledger: fetch entries with status approved")
    ap.add_argument("--lib", default="project", help="library name (default: project)")
    ap.add_argument("--backend", choices=sorted(BACKENDS),
                    help="asset downloader (default: %s; %s is the AGPL-3.0 "
                         "fallback, removed after one release)"
                         % (DEFAULT_BACKEND, FALLBACK_BACKEND))
    ap.add_argument("--report", type=Path, help="write JSON report here too")
    args = ap.parse_args(argv)

    project = args.project.resolve()
    if not project.is_dir():
        print(f"error: {project} is not a directory", file=err)
        return 1

    backend, exe, note = choose_backend(args.backend, find=find)
    if backend is None:
        print(note, file=err)
        return 1
    if note:
        print(note, file=err)

    jobs: list[tuple[str, dict | None]] = [(l, None) for l in args.lcsc]
    ledger = None
    if args.parts:
        ledger = load_ledger(args.parts)
        for entry in ledger["parts"]:
            if entry.get("status") in ("approved", "proposed") and entry.get("lcsc"):
                jobs.append((entry["lcsc"], entry))
    if not jobs:
        print("error: nothing to fetch (--lcsc ids or --parts with approved entries)",
              file=err)
        return 1

    results = []
    for lcsc_id, entry in jobs:
        res = run_backend(project, args.lib, lcsc_id, backend, exe, runner=runner)
        results.append(res)
        if entry is not None and res["ok"]:
            entry["status"] = "fetched"
            entry["symbol"] = res.get("symbol")
            entry["footprint"] = res.get("footprint")
            entry["model_step"] = res.get("model_step")
            entry["fetched_at"] = datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds")
        print(f"{'OK ' if res['ok'] else 'FAIL'} {lcsc_id} "
              f"{res.get('footprint', '')}", file=err)

    ensure_lib_table(project, args.lib)
    if ledger is not None and args.parts:
        args.parts.write_text(json.dumps(ledger, indent=2) + "\n")

    report = {"project": str(project), "lib": args.lib, "backend": backend,
              "results": results, "ok": all(r["ok"] for r in results)}
    print(json.dumps(report, indent=2), file=out)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
