#!/usr/bin/env bash
# Create/refresh build/mcp-venv for the phicad MCP server.
#
# The MCP stack needs protobuf>=6-compatible deps, while kicad-python (build/venv) pins
# protobuf<6 — so the MCP server gets its OWN venv and shells out to build/venv instead of
# importing kipy. Never merge the two.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

# Source and build routinely live in DIFFERENT worktrees, so every path the server needs is
# overridable with the same variables server.py reads (plus PHICAD_MCP_VENV for this venv).
# Without this, a checkout with no build/ reported paths that do not exist anywhere.
# `phicad mcp setup` exports all of these (export_layout); the defaults below only matter when
# this script is run by hand, and they mirror phicad/pylib/build_layout.py.
case "$(uname -s)" in Darwin) plat=mac ;; Linux) plat=linux ;; *) plat=other ;; esac
build="${PHICAD_BUILD:-$repo/build/$plat-dev}"
autovenv="${PHICAD_VENV:-$repo/build/venv}"
venv="${PHICAD_MCP_VENV:-$repo/build/mcp-venv}"
if [[ "$plat" == mac || -d "$build/kicad/PhiCad.app" ]]; then
  cli="${PHICAD_KICAD_CLI:-$build/kicad/PhiCad.app/Contents/MacOS/kicad-cli}"
else
  cli="${PHICAD_KICAD_CLI:-$build/kicad/kicad-cli}"
fi

# PhiCad's LLM proxy env breaks pip/PyPI; strip it for every network call.
nonet() { env -u HTTPS_PROXY -u https_proxy -u ALL_PROXY -u all_proxy \
              -u HTTP_PROXY -u http_proxy "$@"; }

# fastmcp needs >= 3.10; macOS system python3 is 3.9 (CommandLineTools). Prefer Homebrew.
pick_py() {
  if [[ -n "${PYTHON:-}" ]]; then echo "$PYTHON"; return; fi
  local c
  for c in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           /opt/homebrew/bin/python3.11 python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      command -v "$c"; return
    fi
  done
  return 1
}
py="$(pick_py)" || { echo "need python >= 3.10 (set PYTHON=/path/to/python3.x)" >&2; exit 1; }
echo "== python: $py ($("$py" -V 2>&1))"

if [[ ! -x "$venv/bin/python" ]]; then
  echo "== creating $venv"
  "$py" -m venv "$venv"
else
  echo "== reusing $venv"
fi

echo "== installing requirements (proxy env stripped)"
nonet "$venv/bin/python" -m pip install --upgrade pip >/dev/null
nonet "$venv/bin/python" -m pip install -r "$here/requirements.txt"

echo "== verifying imports"
PYTHONPATH="$here" "$venv/bin/python" - <<'PY'
import importlib.metadata as md
import fastmcp, mcp  # noqa: F401
print("fastmcp", fastmcp.__version__)
print("mcp     ", md.version("mcp"))
import server  # noqa: F401  (import-checks the server module itself)
import asyncio; print("tools   ", len(asyncio.run(server.mcp.get_tools())))
PY

[[ -x "$cli" ]] && echo "== kicad-cli: $cli" \
                || echo "!! kicad-cli missing ($cli) — run: phicad/bin/phicad build kicad-cli" \
                        "(or set PHICAD_BUILD/PHICAD_KICAD_CLI)" >&2
[[ -x "$autovenv/bin/python" ]] && echo "== automation venv: $autovenv" \
                || echo "!! automation venv missing ($autovenv) — run: phicad/bin/phicad venv" \
                        "(board_summary/kipy_exec need it), or set PHICAD_VENV" >&2

cat <<EOF

done.

register with your MCP client (writes only the 'phicad' key of ~/.pi/agent/mcp.json,
backs the file up first; --dry-run to preview):

  python3 $here/register.py --dry-run \\
      --build $build --venv $autovenv --mcp-venv $venv
  python3 $here/register.py --build $build --venv $autovenv --mcp-venv $venv

check the whole setup (paths, venvs, kipy fork bindings, client config, stale api-servers):

  python3 $here/doctor.py          # add --json for machine-readable output

smoke test:  $venv/bin/python $here/test_handshake.py
EOF
