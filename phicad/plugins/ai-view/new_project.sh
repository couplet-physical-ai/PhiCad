#!/bin/bash
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Entrypoint for the "New AI Project" action (scope project_manager, runtime type "exec").
#
# The project manager execs this with cwd = the plugin directory and KICAD_API_SOCKET /
# KICAD_API_TOKEN injected, exactly like ai_view.sh for the PCB-scoped action.  The
# interpreter requirements are the same too -- tkinter for the window, kipy so the finished
# project can be opened over IPC -- so the resolution and vetting logic is a copy of
# ai_view.sh's (see there for the full why, including why Apple's Tk 8.5 python must be
# refused rather than allowed to hang).
set -u

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

vet() {
  "$1" - <<'PYVET' >/dev/null 2>&1
import sys, tkinter
if tkinter.TkVersion < 8.6:
    sys.exit(3)
import kipy
PYVET
}

if [ -n "$PY" ] && ! vet "$PY"; then
  echo "new-project: $PY is unusable (needs Tk >= 8.6 and kipy)" >&2
  PY=""
fi

if [ -z "$PY" ]; then
  sys_py="$(command -v python3 || true)"
  if [ -n "$sys_py" ] && vet "$sys_py"; then
    PY="$sys_py"
  fi
fi

if [ -z "$PY" ]; then
  msg="New AI Project cannot start: no Python with both kipy and Tk >= 8.6.

Fix: run  phicad/bin/phicad ai-view-install
or set PHICAD_AI_PYTHON to the repo's build/venv/bin/python."
  echo "new-project: $msg" >&2
  command -v osascript >/dev/null 2>&1 \
    && osascript -e "display alert \"PhiCad New Project\" message \"$msg\" as critical" \
         >/dev/null 2>&1
  exit 1
fi

exec "$PY" "$HERE/new_project_gui.py" "$@"
