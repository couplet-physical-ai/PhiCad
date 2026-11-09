#!/bin/bash
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Entrypoint for the AI View plugin (manifest runtime type "exec").
#
# KiCad execs this file directly with cwd = the plugin directory and with
# KICAD_API_SOCKET / KICAD_API_TOKEN already injected (common/api/api_plugin_manager.cpp).
# Its only job is to find a Python that has BOTH tkinter and kipy and hand over to
# ai_view_gui.py.  Runtime type "python" is deliberately NOT used: it would create a
# fresh venv and pip-install kipy from the network, but this fork needs the
# REGENERATED kipy bindings that live in the repo's build/venv.
#
# Interpreter resolution:
#   1. $PHICAD_AI_PYTHON                    explicit override
#   2. interpreter recorded in ./venv-path   written by the installer (see below)
#   3. <repo>/build/venv/bin/python         found by walking up from the REAL location
#                                           of this script
#   4. python3 on PATH                      last resort -- and it must be VETTED, see below
#
# Why step 2 exists.  The plugin directory must be a REAL directory, not a symlink into the
# repo: wxDir::Traverse does not follow symlinked directories, so a symlinked plugin is never
# discovered and the toolbar button silently never appears.  But once it is a real copy under
# ~/Documents/KiCad/<ver>/plugins/, walking up from it never reaches the repo, so step 3 finds
# nothing and we fall through to /usr/bin/python3.
#
# That fallback is not merely degraded, it HANGS.  Apple's system python3 ships Tk 8.5, and
# under 8.5 on macOS this GUI blocks forever inside Tk's C-level update() -- the window opens
# and paints nothing at all, which looks exactly like "clicking the button does nothing".
# So the last resort is now vetted for Tk >= 8.6 AND kipy, and refuses loudly instead of
# opening a window that will hang.
set -u

# Resolve symlinks to find the real script location (readlink -f is not on stock
# macOS bash 3.2 paths; loop instead).
SRC="${BASH_SOURCE[0]}"
while [ -L "$SRC" ]; do
  DIR="$(cd -P "$(dirname "$SRC")" >/dev/null 2>&1 && pwd)"
  SRC="$(readlink "$SRC")"
  [[ "$SRC" != /* ]] && SRC="$DIR/$SRC"
done
HERE="$(cd -P "$(dirname "$SRC")" >/dev/null 2>&1 && pwd)"

PY=""

if [ -n "${PHICAD_AI_PYTHON:-}" ] && [ -x "${PHICAD_AI_PYTHON}" ]; then
  PY="$PHICAD_AI_PYTHON"
fi

# Recorded by the installer: an absolute interpreter path, so a real-directory copy of this
# plugin can still find the repo's venv.
if [ -z "$PY" ] && [ -f "$HERE/venv-path" ]; then
  cand="$(cat "$HERE/venv-path")"
  [ -x "$cand" ] && PY="$cand"
fi

if [ -z "$PY" ]; then
  d="$HERE"
  while [ "$d" != "/" ]; do
    if [ -x "$d/build/venv/bin/python" ]; then
      PY="$d/build/venv/bin/python"
      break
    fi
    d="$(dirname "$d")"
  done
fi

# Vet whatever we ended up with.  A python without kipy cannot read the board, and a python
# with Tk 8.5 hangs on macOS instead of drawing -- both must fail loudly here rather than
# produce a blank window the user can only interpret as "the button is broken".
vet() {
  "$1" - <<'PYVET' >/dev/null 2>&1
import sys, tkinter
if tkinter.TkVersion < 8.6:
    sys.exit(3)
import kipy
PYVET
}

if [ -n "$PY" ] && ! vet "$PY"; then
  echo "ai-view: $PY is unusable (needs Tk >= 8.6 and kipy)" >&2
  PY=""
fi

if [ -z "$PY" ]; then
  sys_py="$(command -v python3 || true)"
  if [ -n "$sys_py" ] && vet "$sys_py"; then
    PY="$sys_py"
  fi
fi

if [ -z "$PY" ]; then
  msg="AI View cannot start: no Python with both kipy and Tk >= 8.6.

Apple's /usr/bin/python3 ships Tk 8.5, which hangs instead of drawing on macOS,
and it has no kipy, so it cannot read the board.

Fix: run  phicad/bin/phicad ai-view-install
or set PHICAD_AI_PYTHON to the repo's build/venv/bin/python."
  echo "ai-view: $msg" >&2
  # The user clicked a toolbar button and is watching the screen, not a log, so say it there.
  command -v osascript >/dev/null 2>&1 \
    && osascript -e "display alert \"PhiCad AI View\" message \"$msg\" as critical" \
         >/dev/null 2>&1
  exit 1
fi

exec "$PY" "$HERE/ai_view_gui.py" "$@"
