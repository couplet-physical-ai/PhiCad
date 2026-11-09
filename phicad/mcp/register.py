#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Register (or re-point) this worktree's phicad MCP server in an MCP client config.

WHY THIS EXISTS
    Hand-editing `~/.pi/agent/mcp.json` produced an entry whose `command` came from one
    worktree, whose `args[0]` came from a second, and whose `env` pointed at a third through a
    symlink — all of which "looked right".  The MCP stdio client sanitises the child
    environment down to HOME/PATH/USER/SHELL/TERM/LOGNAME, so PHICAD_REPO / PHICAD_BUILD /
    PHICAD_VENV / PHICAD_KICAD_CLI can ONLY reach the server through that `env` block; a shell
    export does nothing.  This script writes one internally consistent entry, checks that each
    path exists before writing, and leaves the rest of the config alone.

WHAT IT TOUCHES
    Only the `phicad` key (under `mcpServers` if that is how the file is organised — the
    layout is preserved).  Everything else is round-tripped through json with indent=2 /
    ensure_ascii=False, the file keeps mode 600 (it may hold bearer tokens), and a timestamped
    backup is written next to it first.

    stdlib only, python >= 3.9 (macOS system python) — it must run before any venv exists.

USAGE
    python3 phicad/mcp/register.py --dry-run          # show the entry, write nothing
    python3 phicad/mcp/register.py                    # write ~/.pi/agent/mcp.json
    python3 phicad/mcp/register.py --build /other/worktree/build/mac-dev \
                                  --venv  /other/worktree/build/venv \
                                  --mcp-venv /other/worktree/build/mcp-venv

    Verify the result with:  python3 phicad/mcp/doctor.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

# Shared with server.py / doctor.py: build/<mac|linux>-dev and where kicad-cli sits in it.
_PYLIB = str(Path(__file__).resolve().parent.parent / "pylib")
if _PYLIB not in sys.path:
    sys.path.append(_PYLIB)
import build_layout  # noqa: E402

DEFAULT_CONFIG = Path.home() / ".pi" / "agent" / "mcp.json"

ENTRY_COMMENT = [
    "PhiCad fork automation: DRC/ERC, exports, diffs, jobsets, managed IPC session,",
    "parts pipeline, layout swarm, design synthesis. Written by phicad/mcp/register.py.",
    "The PHICAD_* env block is REQUIRED: the stdio client sanitises the child env to",
    "HOME/PATH/USER/SHELL/TERM/LOGNAME, so shell exports never reach the server, and",
    "source and build routinely live in different worktrees.",
    "Check the whole setup with: python3 <repo>/phicad/mcp/doctor.py",
]


def resolve_defaults():
    """Defaults derived from this file's location, with the same env overrides as the server."""
    mcp_dir = Path(__file__).resolve().parent                  # <repo>/phicad/mcp
    repo = mcp_dir.parent.parent

    def env_path(name, fallback):
        raw = os.environ.get(name, "").strip()
        return Path(raw).expanduser() if raw else fallback

    build = env_path("PHICAD_BUILD", build_layout.default_build_dir(repo))
    return {
        "repo": repo,
        "build": build,
        "venv": env_path("PHICAD_VENV", repo / "build" / "venv"),
        "mcp_venv": env_path("PHICAD_MCP_VENV", repo / "build" / "mcp-venv"),
        "kicad_cli": env_path("PHICAD_KICAD_CLI", build_layout.kicad_cli_in(build)),
    }


def build_entry(repo, build, venv, mcp_venv, kicad_cli):
    """The exact entry shape the client needs — absolute paths only, nothing cwd-relative."""
    return {
        "_comment": list(ENTRY_COMMENT),
        "type": "stdio",
        "command": str(Path(mcp_venv) / "bin" / "python"),
        "args": [str(Path(repo) / "phicad" / "mcp" / "server.py")],
        "toolMode": "lazy",
        "injectInstructions": False,
        "env": {
            "PHICAD_REPO": str(repo),
            "PHICAD_BUILD": str(build),
            "PHICAD_VENV": str(venv),
            "PHICAD_KICAD_CLI": str(kicad_cli),
        },
    }


def load_config(path):
    """Existing config, or a fresh skeleton. Refuses to guess at unparseable JSON."""
    if not path.exists():
        return {}, True
    try:
        with open(str(path), "r") as handle:
            text = handle.read()
    except OSError as exc:
        print("error: cannot read %s: %s" % (path, exc), file=sys.stderr)
        raise SystemExit(2)
    if not text.strip():
        return {}, True
    try:
        config = json.loads(text)
    except ValueError as exc:
        print("error: %s is not valid JSON (%s) — fix it by hand; refusing to overwrite"
              % (path, exc), file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(config, dict):
        print("error: %s does not hold a JSON object" % path, file=sys.stderr)
        raise SystemExit(2)
    return config, False


def place_entry(config, entry):
    """Set only the phicad key, honouring whichever layout the file already uses."""
    servers = config.get("mcpServers")
    if isinstance(servers, dict):
        servers["phicad"] = entry
        return "mcpServers.phicad"
    if "phicad" in config or not config:
        # Flat layout (or a brand-new file): keep it flat rather than restructuring the user's file.
        config["phicad"] = entry
        return "phicad"
    config["phicad"] = entry
    return "phicad"


def backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + ".bak-" + stamp)
    shutil.copy2(str(path), str(dest))
    try:
        os.chmod(str(dest), stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return dest


def write_config(path, config):
    """Write atomically-ish, then re-assert mode 600 (the file may hold bearer tokens)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with open(str(tmp), "w") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.chmod(str(tmp), stat.S_IRUSR | stat.S_IWUSR)
    os.replace(str(tmp), str(path))
    os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)


def main(argv=None):
    d = resolve_defaults()
    parser = argparse.ArgumentParser(
        description="Write/update only the 'phicad' entry of an MCP client config.")
    parser.add_argument("--repo", default=str(d["repo"]),
                        help="repo root holding phicad/mcp/server.py (default: %(default)s)")
    parser.add_argument("--build", default=str(d["build"]),
                        help="build tree (default: $PHICAD_BUILD or <repo>/build/mac-dev)")
    parser.add_argument("--venv", default=str(d["venv"]),
                        help="automation venv with kipy/kicad_tools (default: <repo>/build/venv)")
    parser.add_argument("--mcp-venv", dest="mcp_venv", default=str(d["mcp_venv"]),
                        help="venv with fastmcp, provides the interpreter "
                             "(default: <repo>/build/mcp-venv)")
    parser.add_argument("--kicad-cli", dest="kicad_cli", default=None,
                        help="kicad-cli binary (default: derived from --build)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="client config to update (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resulting entry and exit; write nothing")
    parser.add_argument("--force", action="store_true",
                        help="write even if command / server.py / kicad-cli is missing")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    build = Path(args.build).expanduser()
    venv = Path(args.venv).expanduser()
    mcp_venv = Path(args.mcp_venv).expanduser()
    kicad_cli = (Path(args.kicad_cli).expanduser() if args.kicad_cli
                 else build_layout.kicad_cli_in(build))
    config_path = Path(args.config).expanduser()

    entry = build_entry(repo, build, venv, mcp_venv, kicad_cli)

    # Hard requirements: without these three the entry cannot possibly start.
    problems = []
    for label, path in (("command (mcp venv python)", Path(entry["command"])),
                        ("server.py", Path(entry["args"][0])),
                        ("kicad-cli", kicad_cli)):
        if not path.exists():
            problems.append("%s missing: %s" % (label, path))
    # Soft: the automation venv only breaks the kipy-backed tools, so it is a warning.
    warnings = []
    if not (venv / "bin" / "python").exists():
        warnings.append("automation venv python missing: %s (kipy-backed tools will fail; "
                        "run phicad/bin/phicad venv)" % (venv / "bin" / "python"))

    print("phicad MCP registration -> %s" % config_path)
    print(json.dumps({"phicad": entry}, indent=2, ensure_ascii=False))
    for line in warnings:
        print("warning: %s" % line, file=sys.stderr)

    if problems and not args.force:
        for line in problems:
            print("error: %s" % line, file=sys.stderr)
        print("refusing to write (use --force to override, or pass --build/--mcp-venv/--repo "
              "pointing at the built worktree)", file=sys.stderr)
        return 2
    for line in problems:
        print("warning: %s (--force)" % line, file=sys.stderr)

    if args.dry_run:
        print("\ndry run — %s not modified" % config_path)
        return 0

    config, created = load_config(config_path)
    if not created:
        saved = backup(config_path)
        print("backup: %s" % saved)
    where = place_entry(config, entry)
    write_config(config_path, config)
    print("wrote %s (%s), mode 600" % (config_path, where))
    print("verify: python3 %s" % (repo / "phicad" / "mcp" / "doctor.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
