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
"""phicad MCP doctor — one command that answers "is the MCP surface actually functioning?".

WHY THIS EXISTS
    The MCP server resolves four independent locations (repo, build tree, automation venv,
    kicad-cli) from PHICAD_* env overrides, and a fifth (its own venv) lives in the client
    config.  Source and build routinely sit in DIFFERENT worktrees, and the stdio client
    sanitises the child environment down to HOME/PATH/USER/SHELL/TERM/LOGNAME — so the only
    place those overrides can arrive is the client config's `env` block.  Every failure mode
    of that arrangement (a stale path, a symlinked twin worktree, stock kipy bindings, an
    orphaned api-server) shows up at tool-call time as an unhelpful "missing" message.  This
    script checks all of it up front and prints where the truth is.

DESIGN CONSTRAINTS
    * stdlib only, and it must run on macOS system python 3.9 (/usr/bin/python3) — this is
      the one script a user can always run, before any venv exists.  So: no match statements,
      no `X | Y` runtime annotations, `from __future__ import annotations` for the rest.
    * It does NOT import server.py.  server.py needs fastmcp, which by definition is missing
      in exactly the broken cases this script exists to diagnose; the path-resolution logic is
      deliberately replicated (keep `_path_env` below in sync with server.py's).

USAGE
    python3 phicad/mcp/doctor.py            # human table, exit 1 if any check FAILs
    python3 phicad/mcp/doctor.py --json     # machine-readable, same exit code

    Honours the same overrides as the server — PHICAD_REPO, PHICAD_BUILD, PHICAD_KICAD_CLI,
    PHICAD_VENV — plus PHICAD_MCP_VENV for the MCP venv.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Shared with server.py / register.py: build/<mac|linux>-dev and where kicad-cli sits in it.
_PYLIB = str(Path(__file__).resolve().parent.parent / "pylib")
if _PYLIB not in sys.path:
    sys.path.append(_PYLIB)
import build_layout  # noqa: E402

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

CHECK_TIMEOUT = 60


# --------------------------------------------------------------------------------------
# Layout — MUST mirror server.py's `_path_env` / REPO_ROOT / BUILD_DIR / KICAD_CLI / VENV_PY.
# --------------------------------------------------------------------------------------

def _path_env(name, fallback):
    """An overridable path, exactly as server.py resolves it (env wins, then the default)."""
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else fallback


def resolve_layout():
    """Every location the server will use, derived the way the server derives it."""
    mcp_dir = Path(__file__).resolve().parent               # <repo>/phicad/mcp
    repo_root = _path_env("PHICAD_REPO", mcp_dir.parent.parent)
    build_dir = _path_env("PHICAD_BUILD", build_layout.default_build_dir(repo_root))
    kicad_cli = _path_env("PHICAD_KICAD_CLI", build_layout.kicad_cli_in(build_dir))
    venv_dir = _path_env("PHICAD_VENV", repo_root / "build" / "venv")
    mcp_venv = _path_env("PHICAD_MCP_VENV", repo_root / "build" / "mcp-venv")
    return {
        "mcp_dir": mcp_dir,
        "this_server": mcp_dir / "server.py",
        "repo_root": repo_root,
        "build_dir": build_dir,
        "kicad_cli": kicad_cli,
        "venv": venv_dir,
        "venv_py": venv_dir / "bin" / "python",
        "mcp_venv": mcp_venv,
        "mcp_venv_py": mcp_venv / "bin" / "python",
        "phicad_bin": repo_root / "phicad" / "bin" / "phicad",
        "regen_proto": repo_root / "phicad" / "bin" / "regen-kipy-proto",
    }


def _env_source(name):
    """Where a path came from — worth printing, since a wrong override is the usual bug."""
    return "env %s" % name if os.environ.get(name, "").strip() else "default"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def _run(argv, env=None, timeout=CHECK_TIMEOUT):
    """Run a command, never raise; returns (rc, stdout, stderr). rc -1 == could not run."""
    child = dict(os.environ)
    # Never let the LLM proxy leak into a child (it breaks anything doing network I/O).
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                "HTTP_PROXY", "http_proxy"):
        child.pop(var, None)
    if env:
        child.update(env)
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=child, timeout=timeout)
    except FileNotFoundError as exc:
        return -1, "", "not found: %s" % exc
    except subprocess.TimeoutExpired:
        return -1, "", "timed out after %ss" % timeout
    except OSError as exc:
        return -1, "", "%s: %s" % (type(exc).__name__, exc)
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    return proc.returncode, out, err


def _first_line(text):
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _same_file(a, b):
    """realpath comparison that tolerates either side not existing (symlinked worktrees!)."""
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return False


class Report:
    """Collected results: a check is (status, name, detail, hint)."""

    def __init__(self):
        self.checks = []

    def add(self, status, name, detail, hint=""):
        self.checks.append({"status": status, "name": name, "detail": detail, "hint": hint})
        return status

    def worst(self):
        if any(c["status"] == FAIL for c in self.checks):
            return FAIL
        if any(c["status"] == WARN for c in self.checks):
            return WARN
        return OK

    def counts(self):
        return {
            "ok": sum(1 for c in self.checks if c["status"] == OK),
            "warn": sum(1 for c in self.checks if c["status"] == WARN),
            "fail": sum(1 for c in self.checks if c["status"] == FAIL),
        }


# --------------------------------------------------------------------------------------
# (a) resolved layout
# --------------------------------------------------------------------------------------

def check_layout(rep, L):
    for label, key, env_name, must_exist in (
            ("repo root", "repo_root", "PHICAD_REPO", True),
            ("build dir", "build_dir", "PHICAD_BUILD", True),
            ("automation venv", "venv", "PHICAD_VENV", True),
            ("mcp venv", "mcp_venv", "PHICAD_MCP_VENV", False),
    ):
        path = L[key]
        exists = path.exists()
        detail = "%s  [%s]" % (path, _env_source(env_name))
        if exists:
            rep.add(OK, "path: " + label, detail)
        else:
            rep.add(FAIL if must_exist else WARN, "path: " + label, detail + "  (MISSING)",
                    "set %s to the worktree that really holds it" % env_name)

    # The repo root must be the one that owns this server.py, or PHICAD_REPO is pointing at a twin.
    server_here = L["this_server"]
    claimed = L["repo_root"] / "phicad" / "mcp" / "server.py"
    if _same_file(server_here, claimed):
        rep.add(OK, "path: repo owns this server.py", str(server_here))
    else:
        rep.add(WARN, "path: repo owns this server.py",
                "this file: %s  !=  PHICAD_REPO's: %s" % (server_here, claimed),
                "PHICAD_REPO points at a different worktree than the server being run")


# --------------------------------------------------------------------------------------
# (b) kicad-cli
# --------------------------------------------------------------------------------------

def check_kicad_cli(rep, L, out):
    cli = L["kicad_cli"]
    if not cli.exists():
        rep.add(FAIL, "kicad-cli", "%s  (MISSING)  [%s]" % (cli, _env_source("PHICAD_KICAD_CLI")),
                "phicad/bin/phicad build kicad-cli, or point PHICAD_BUILD/PHICAD_KICAD_CLI "
                "at the built worktree")
        return
    if not os.access(str(cli), os.X_OK):
        rep.add(FAIL, "kicad-cli", "%s  (not executable)" % cli, "chmod +x it")
        return
    rc, sout, serr = _run([str(cli), "version"], env={"KICAD_RUN_FROM_BUILD_DIR": "1"})
    version = _first_line(sout) or _first_line(serr)
    if rc == 0 and version:
        out["kicad_cli_version"] = version
        rep.add(OK, "kicad-cli runs", "%s  ->  version %s" % (cli, version))
    else:
        rep.add(FAIL, "kicad-cli runs", "exit %s: %s" % (rc, _first_line(serr) or version),
                "run it by hand with KICAD_RUN_FROM_BUILD_DIR=1; a build-tree binary "
                "refuses to start without that flag")


# --------------------------------------------------------------------------------------
# (c) automation venv — kipy + kicad_tools + the FORK protobuf bindings
# --------------------------------------------------------------------------------------

KIPY_PROBE = (
    "import json, sys\n"
    "res = {}\n"
    "try:\n"
    "    import importlib.metadata as md\n"
    "except Exception:\n"
    "    md = None\n"
    "def _ver(pkg, mod):\n"
    "    if md is not None:\n"
    "        try:\n"
    "            return md.version(pkg)\n"
    "        except Exception:\n"
    "            pass\n"
    "    return getattr(mod, '__version__', 'unknown')\n"
    "try:\n"
    "    import kipy, kicad_tools\n"
    "    res['kipy'] = _ver('kicad-python', kipy)\n"
    "    res['kicad_tools'] = _ver('kicad-tools', kicad_tools)\n"
    "except Exception as exc:\n"
    "    res['import_error'] = '%s: %s' % (type(exc).__name__, exc)\n"
    "try:\n"
    "    from kipy.proto.common.commands.project_commands_pb2 import OpenDocument\n"
    "    res['fork_proto'] = True\n"
    "except Exception as exc:\n"
    "    res['fork_proto'] = False\n"
    "    res['fork_proto_error'] = '%s: %s' % (type(exc).__name__, exc)\n"
    "sys.stdout.write(json.dumps(res))\n"
)


def check_automation_venv(rep, L, out):
    py = L["venv_py"]
    if not py.exists():
        rep.add(FAIL, "automation venv python", "%s  (MISSING)" % py,
                "phicad/bin/phicad venv  (board_summary / kipy_exec / parts / swarm need it)")
        return
    rc, sout, serr = _run([str(py), "-c", KIPY_PROBE])
    try:
        res = json.loads(sout) if sout else {}
    except ValueError:
        res = {}
    if rc != 0 or not res:
        rep.add(FAIL, "automation venv imports", "exit %s: %s" % (rc, _first_line(serr)),
                "phicad/bin/phicad venv")
        return
    if "import_error" in res:
        rep.add(FAIL, "automation venv imports", "kipy/kicad_tools: " + res["import_error"],
                "phicad/bin/phicad venv")
    else:
        out["kipy_version"] = res.get("kipy", "unknown")
        out["kicad_tools_version"] = res.get("kicad_tools", "unknown")
        rep.add(OK, "automation venv imports", "%s  ->  kipy %s, kicad_tools %s"
                % (py, res.get("kipy"), res.get("kicad_tools")))
    if res.get("fork_proto"):
        out["kipy_fork_proto"] = True
        rep.add(OK, "kipy fork protobuf bindings", "OpenDocument importable (fork commands present)")
    else:
        out["kipy_fork_proto"] = False
        rep.add(FAIL, "kipy fork protobuf bindings",
                res.get("fork_proto_error", "OpenDocument not importable"),
                "run %s  (stock kipy 0.8 ships stable-KiCad bindings, so OpenDocument cannot "
                "even be constructed)" % L["regen_proto"])


# --------------------------------------------------------------------------------------
# (d) mcp venv — fastmcp
# --------------------------------------------------------------------------------------

FASTMCP_PROBE = (
    "import json, sys\n"
    "res = {}\n"
    "try:\n"
    "    import fastmcp\n"
    "    res['fastmcp'] = getattr(fastmcp, '__version__', 'unknown')\n"
    "except Exception as exc:\n"
    "    res['error'] = '%s: %s' % (type(exc).__name__, exc)\n"
    "try:\n"
    "    import importlib.metadata as md\n"
    "    res['mcp'] = md.version('mcp')\n"
    "except Exception:\n"
    "    pass\n"
    "sys.stdout.write(json.dumps(res))\n"
)


def check_mcp_venv(rep, L, out):
    py = L["mcp_venv_py"]
    if not py.exists():
        # Reported, not resolved: finding a sibling worktree's venv is the client config's job.
        rep.add(FAIL, "mcp venv python", "%s  (MISSING)  [%s]"
                % (py, _env_source("PHICAD_MCP_VENV")),
                "bash phicad/mcp/setup.sh, or set PHICAD_MCP_VENV to the built worktree's "
                "build/mcp-venv")
        return
    rc, sout, serr = _run([str(py), "-c", FASTMCP_PROBE])
    try:
        res = json.loads(sout) if sout else {}
    except ValueError:
        res = {}
    if rc != 0 or "fastmcp" not in res:
        rep.add(FAIL, "mcp venv imports fastmcp",
                res.get("error") or ("exit %s: %s" % (rc, _first_line(serr))),
                "bash phicad/mcp/setup.sh")
        return
    out["fastmcp_version"] = res["fastmcp"]
    detail = "%s  ->  fastmcp %s" % (py, res["fastmcp"])
    if res.get("mcp"):
        detail += ", mcp %s" % res["mcp"]
        out["mcp_version"] = res["mcp"]
    rep.add(OK, "mcp venv imports fastmcp", detail)


# --------------------------------------------------------------------------------------
# (e) autoroute — java + Freerouting jar (OPTIONAL: warn, never fail)
# --------------------------------------------------------------------------------------

def check_autoroute(rep, out):
    rc, sout, serr = _run(["java", "-version"], timeout=30)
    if rc == 0:
        # `java -version` writes to stderr on most JDKs.
        out["java_version"] = _first_line(serr) or _first_line(sout)
        rep.add(OK, "java (autoroute)", out["java_version"])
    else:
        rep.add(WARN, "java (autoroute)", "not runnable: %s" % (_first_line(serr) or rc),
                "brew install openjdk  (only needed for phicad autoroute)")

    env_jar = os.environ.get("FREEROUTING_JAR", "").strip()
    default_jar = Path.home() / ".kicad-mcp" / "freerouting.jar"
    jar = Path(env_jar).expanduser() if env_jar else default_jar
    if jar.is_file():
        out["freerouting_jar"] = str(jar)
        rep.add(OK, "freerouting jar", "%s (%.1f MB)  [%s]"
                % (jar, jar.stat().st_size / 1e6,
                   "env FREEROUTING_JAR" if env_jar else "default ~/.kicad-mcp"))
    else:
        rep.add(WARN, "freerouting jar", "%s  (MISSING)" % jar,
                "download freerouting.jar to ~/.kicad-mcp/ or set FREEROUTING_JAR "
                "(only needed for phicad autoroute)")


# --------------------------------------------------------------------------------------
# (f) the client config — where the PHICAD_* overrides must actually live
# --------------------------------------------------------------------------------------

DEFAULT_CONFIG = Path.home() / ".pi" / "agent" / "mcp.json"

# The stdio client sanitises the child env down to these, so nothing else propagates.
SANITISED_ENV = ("HOME", "PATH", "USER", "SHELL", "TERM", "LOGNAME")


def find_entry(config):
    """The 'phicad' entry, wherever this client keeps it. Returns (entry, location) or (None, '')."""
    if not isinstance(config, dict):
        return None, ""
    servers = config.get("mcpServers")
    if isinstance(servers, dict) and isinstance(servers.get("phicad"), dict):
        return servers["phicad"], "mcpServers.phicad"
    if isinstance(config.get("phicad"), dict):
        return config["phicad"], "phicad"
    return None, ""


def check_client_config(rep, L, out, config_path):
    fix = ("python3 %s/phicad/mcp/register.py --build %s --venv %s --mcp-venv %s"
           % (L["mcp_dir"].parent.parent, L["build_dir"], L["venv"], L["mcp_venv"]))
    if not config_path.exists():
        rep.add(WARN, "client config", "%s  (MISSING)" % config_path, fix)
        return
    try:
        with open(str(config_path), "r") as handle:
            config = json.load(handle)
    except ValueError as exc:
        rep.add(FAIL, "client config", "%s: invalid JSON: %s" % (config_path, exc),
                "fix the JSON by hand; register.py refuses to touch a file it cannot parse")
        return
    except OSError as exc:
        rep.add(FAIL, "client config", "%s: %s" % (config_path, exc))
        return

    entry, where = find_entry(config)
    if entry is None:
        rep.add(FAIL, "client config: phicad entry", "no 'phicad' server in %s" % config_path, fix)
        return

    info = {"config": str(config_path), "location": where}
    out["client_entry"] = info
    rep.add(OK, "client config: phicad entry", "%s  ->  %s" % (config_path, where))

    # command — the interpreter, which must be the MCP venv's python.
    command = entry.get("command", "")
    info["command"] = command
    if not command:
        rep.add(FAIL, "config: command", "absent", fix)
    elif Path(command).exists():
        note = ""
        if not _same_file(command, L["mcp_venv_py"]):
            note = "  (resolved mcp venv here is %s)" % L["mcp_venv_py"]
        rep.add(OK, "config: command", command + note)
    else:
        rep.add(FAIL, "config: command", "%s  (MISSING)" % command, fix)

    # args[0] — the server.py that will really be executed.
    args = entry.get("args") or []
    info["args"] = args
    if not args:
        rep.add(FAIL, "config: args[0] server.py", "absent", fix)
    else:
        target = Path(str(args[0]))
        info["server_py"] = str(target)
        if not target.exists():
            rep.add(FAIL, "config: args[0] server.py", "%s  (MISSING)" % target, fix)
        elif _same_file(target, L["this_server"]):
            rep.add(OK, "config: args[0] server.py", "%s  (== this repo's)" % target)
        else:
            # Name both checkouts' git heads so "a different checkout" also says
            # HOW different — a registered server that trails a release looks
            # healthy in every other check while missing the newest fixes.
            drift = _checkout_drift(target, L["this_server"])
            rep.add(WARN, "config: args[0] server.py",
                    "%s  !=  this repo's %s%s" % (target, L["this_server"], drift),
                    "the client is running a DIFFERENT checkout's server; to point it here: "
                    + fix)

    # env — the only channel for PHICAD_*, because the client sanitises everything else.
    env_block = entry.get("env") or {}
    info["env"] = dict(env_block)
    missing = [k for k in ("PHICAD_REPO", "PHICAD_BUILD", "PHICAD_VENV", "PHICAD_KICAD_CLI")
               if not str(env_block.get(k, "")).strip()]
    if missing:
        rep.add(WARN, "config: env block",
                "not set: %s  (client passes only %s, so the server falls back to paths "
                "derived from its own __file__)" % (", ".join(missing), "/".join(SANITISED_ENV)),
                fix)
    else:
        rep.add(OK, "config: env block", "all four PHICAD_* overrides present")

    bad = []
    for key in sorted(env_block):
        value = str(env_block[key])
        if not key.startswith("PHICAD_"):
            continue
        if not value.startswith("/"):
            continue          # $VAR / ${VAR} indirection is the client's business, not ours
        if not Path(value).exists():
            bad.append("%s=%s" % (key, value))
    if bad:
        rep.add(FAIL, "config: env paths exist", "MISSING: " + "; ".join(bad), fix)
    elif env_block:
        rep.add(OK, "config: env paths exist",
                ", ".join("%s=%s" % (k, env_block[k]) for k in sorted(env_block)))

    # A config whose env disagrees with args[0]'s own repo is the exact inconsistency that
    # produced "automation venv missing" for every interesting tool.
    repo_in_env = str(env_block.get("PHICAD_REPO", "")).strip()
    if repo_in_env and args:
        expected = Path(repo_in_env) / "phicad" / "mcp" / "server.py"
        if not _same_file(expected, Path(str(args[0]))):
            rep.add(WARN, "config: PHICAD_REPO vs args[0]",
                    "PHICAD_REPO=%s does not own %s" % (repo_in_env, args[0]), fix)
        else:
            rep.add(OK, "config: PHICAD_REPO vs args[0]", "consistent (%s)" % repo_in_env)


def _git_head(path):
    """Short HEAD of the checkout owning `path`, or '' — never raises, never fetches."""
    rc, sout, _ = _run(["git", "-C", str(Path(path).parent), "rev-parse", "--short", "HEAD"],
                       timeout=15)
    return sout.strip() if rc == 0 and sout.strip() else ""


def _checkout_drift(their_server_py, our_server_py):
    """'  (their HEAD abc123, this repo def456)' when both resolve, else ''."""
    theirs = _git_head(their_server_py)
    ours = _git_head(our_server_py)
    if not theirs or not ours:
        return ""
    if theirs == ours:
        return "  (same commit %s — a worktree twin, not a drift)" % theirs
    return "  (their HEAD %s, this repo %s — one of them trails)" % (theirs, ours)


#: Suffixes a server legitimately creates on first write, so "missing" is not "broken"
#: as long as the directory exists.  Deliberately short: anything not listed is still
#: reported, because the check exists to catch a path that no longer resolves.
_CREATED_ON_WRITE = (".log", ".jsonl", ".ndjson", ".sock", ".pid", ".db", ".sqlite",
                    ".sqlite3")


def check_other_servers(rep, out, config_path):
    """Dangling paths in every OTHER registered server — the breakage nothing else owns.

    Two real incidents motivated this: an entry whose env paths pointed at a deleted
    worktree (tools failed while the server initialised fine), and an entry silently
    dropped by a config rewrite.  The drop cannot be detected generically, but a path
    that no longer exists can — and it is always a bug SOMEWHERE (the config or the
    filesystem), never a healthy state.  WARN, not FAIL: a foreign server's contract
    is unknown here, and someone else's broken entry must not mark THIS server BROKEN.
    """
    try:
        with open(str(config_path), "r") as handle:
            servers = (json.load(handle) or {}).get("mcpServers") or {}
    except (OSError, ValueError):
        return  # check_client_config already reported the file itself
    if not isinstance(servers, dict):
        return

    audited, findings = [], []
    for name in sorted(servers):
        if name == "phicad":
            continue  # covered in depth by check_client_config
        entry = servers.get(name)
        if not isinstance(entry, dict):
            findings.append("%s: entry is not an object" % name)
            continue
        if entry.get("url"):
            continue  # remote server: reachability is an online question, not a path one
        problems = []
        command = str(entry.get("command", ""))
        if command and command.startswith("/") and not Path(command).exists():
            problems.append("command %s" % command)
        for arg in entry.get("args") or []:
            arg = str(arg)
            if arg.startswith("/") and not Path(arg).exists():
                problems.append("arg %s" % arg)
        for key, value in sorted((entry.get("env") or {}).items()):
            value = str(value)
            # A URL is not a path. Splitting one on ":" to walk a PATH-style list turns
            # `http://host:7125` into the head `//host`, which starts with "/" and does not
            # exist -- reported as a dangling path on every run.
            if "://" in value:
                continue
            # Path lists (PATH, comma-separated search paths) are judged head by head.
            for head in (h for part in value.split(",") for h in part.split(":")):
                if not head.startswith("/") or Path(head).exists():
                    continue
                # A file the server CREATES on first write (audit log, socket, sqlite db) is
                # not dangling as long as its directory is there, and warning about it trains
                # the reader to ignore this check. A missing path whose parent is also missing,
                # or any other missing name, still breaks the server and is still reported --
                # that is what caught a real `KICAD_APP_PATH` pointing into a retired worktree.
                if Path(head).suffix.lower() in _CREATED_ON_WRITE and Path(head).parent.is_dir():
                    continue
                problems.append("env %s -> %s" % (key, head))
        audited.append(name)
        if problems:
            findings.append("%s: %s" % (name, "; ".join(problems)))

    out["other_servers"] = {"audited": audited, "problems": findings}
    if not audited and not findings:
        return  # nothing but phicad/remotes registered — nothing to say
    if findings:
        rep.add(WARN, "other registered servers",
                "dangling paths — " + " | ".join(findings),
                "fix or re-register the named entries in %s (a path that no longer exists "
                "breaks that server's tools even when it still initialises)" % config_path)
    else:
        rep.add(OK, "other registered servers",
                "%d local entr%s path-checked: %s"
                % (len(audited), "y" if len(audited) == 1 else "ies", ", ".join(audited)))


# --------------------------------------------------------------------------------------
# (g) stale/orphaned api-server children
# --------------------------------------------------------------------------------------

def check_api_servers(rep, out):
    rc, sout, _ = _run(["pgrep", "-fl", "kicad-cli api-server"], timeout=30)
    lines = [ln for ln in sout.splitlines() if ln.strip()] if rc == 0 else []
    procs = []
    for line in lines:
        parts = line.strip().split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == os.getpid():
            continue
        prc, pout, _ = _run(["ps", "-o", "ppid=", "-p", str(pid)], timeout=30)
        ppid = -1
        if prc == 0 and pout.strip().isdigit():
            ppid = int(pout.strip())
        procs.append({"pid": pid, "ppid": ppid,
                      "command": parts[1] if len(parts) > 1 else "",
                      "orphan": ppid == 1})
    out["api_servers"] = procs
    if not procs:
        rep.add(OK, "stale api-server processes", "none running")
        return
    orphans = [p for p in procs if p["orphan"]]
    listing = "; ".join("pid %d ppid %d" % (p["pid"], p["ppid"]) for p in procs)
    if orphans:
        # Do NOT advise a bare `kill`: `phicad api-server` is normally backgrounded and its
        # launcher shell exits, so a perfectly healthy server that the web panel and kipy are
        # talking to also shows ppid 1. api_server_reap_orphans only reaps the ones whose
        # socket is unreachable, and defaults to a dry run.
        rep.add(WARN, "stale api-server processes",
                "%d running, %d ORPHANED (ppid 1): %s" % (len(procs), len(orphans), listing),
                "api_server_reap_orphans() to list them, then dry_run=false to reap the "
                "unreachable ones (ppid 1 alone is normal for a backgrounded server, so do "
                "not kill by pid blindly)")
    else:
        rep.add(WARN, "stale api-server processes",
                "%d running (all parented): %s" % (len(procs), listing),
                "expected if an MCP server session is live; otherwise stop it with "
                "api_server_stop()")


# --------------------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------------------

def print_table(rep, L, out, config_path):
    width = max(len(c["name"]) for c in rep.checks) if rep.checks else 10
    print("phicad MCP doctor")
    print("  this server.py : %s" % L["this_server"])
    print("  client config  : %s" % config_path)
    print("  python         : %s (%s)" % (".".join(str(v) for v in sys.version_info[:3]),
                                          sys.executable))
    print("")
    print("%-4s  %-*s  %s" % ("", width, "CHECK", "DETAIL"))
    print("%-4s  %-*s  %s" % ("----", width, "-" * width, "-" * 40))
    for check in rep.checks:
        print("%-4s  %-*s  %s" % (check["status"], width, check["name"], check["detail"]))
    hints = [c for c in rep.checks if c["status"] != OK and c["hint"]]
    if hints:
        print("")
        print("fixes:")
        for check in hints:
            print("  [%s] %s" % (check["status"], check["name"]))
            print("        %s" % check["hint"])
    counts = rep.counts()
    print("")
    print("%d ok, %d warn, %d fail  ->  %s"
          % (counts["ok"], counts["warn"], counts["fail"],
             "FUNCTIONING" if rep.worst() != FAIL else "BROKEN"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Diagnose the phicad MCP server's setup (paths, venvs, client config).")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="MCP client config to inspect (default: %s)" % DEFAULT_CONFIG)
    args = parser.parse_args(argv)

    L = resolve_layout()
    rep = Report()
    out = {}
    config_path = Path(args.config).expanduser()

    check_layout(rep, L)
    check_kicad_cli(rep, L, out)
    check_automation_venv(rep, L, out)
    check_mcp_venv(rep, L, out)
    check_autoroute(rep, out)
    check_client_config(rep, L, out, config_path)
    check_other_servers(rep, out, config_path)
    check_api_servers(rep, out)

    status = rep.worst()
    if args.as_json:
        payload = {
            "ok": status != FAIL,
            "status": status,
            "summary": rep.counts(),
            "python": {"version": ".".join(str(v) for v in sys.version_info[:3]),
                       "executable": sys.executable},
            "paths": dict((k, str(v)) for k, v in L.items()),
            "path_sources": dict(
                (name, _env_source(name)) for name in
                ("PHICAD_REPO", "PHICAD_BUILD", "PHICAD_KICAD_CLI", "PHICAD_VENV",
                 "PHICAD_MCP_VENV")),
            "config_path": str(config_path),
            "checks": rep.checks,
            "facts": out,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_table(rep, L, out, config_path)
    return 1 if status == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
