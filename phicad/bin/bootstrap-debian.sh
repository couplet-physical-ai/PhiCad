#!/usr/bin/env bash
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# phicad/bin/bootstrap-debian.sh — install the PhiCad build dependencies on a Debian/Ubuntu
# host (qualified on Debian 13 "trixie"). Run it locally through `phicad bootstrap`, or on a
# remote host with:
#   ssh <host> 'bash -s' < phicad/bin/bootstrap-debian.sh
#
# Idempotent: every step checks before it acts and re-running is always safe. Nothing here
# touches credentials, so this script can be pasted, logged and reviewed freely.
#
# What it installs (all from Debian's own archive, no third-party repos):
#   * the KiCad 10 toolchain: cmake, ninja, ccache, gcc, swig, protobuf, nng, wxWidgets 3.2,
#     Boost, OpenCascade, ngspice, libgit2, cairo/pixman, GLEW/GLM, curl, zstd, gettext.
#     PHICAD_BOOTSTRAP_BUILD_DEP=1 additionally runs `apt-get build-dep kicad` (Debian's
#     9.0.x packaging) — off by default because it drags in TeX Live for the docs (~1.5 GB).
#   * default-jre-headless: Freerouting (phicad autoroute) is a Java program.
#   * gh: so the host can open/inspect pull requests once the user authenticates it.
#   * python3-venv/pip: `phicad venv` and the MCP server venv.
#
# Why apt and not Homebrew/conda: the deployment target for a hosted PhiCad is a Linux
# container built from the distro archive; keeping the build host identical to that target is
# what makes a build here evidence for a deployment there.

set -euo pipefail

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Non-interactive ssh does not read ~/.profile, so user-local tools (node, phi) are invisible
# unless we add them ourselves. Every remote script must do this.
export PATH="$HOME/.local/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

[[ -r /etc/os-release ]] || die "not a Linux host with /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "debian" || "${ID_LIKE:-}" == *debian* ]] \
  || die "this bootstrap is qualified for Debian/Ubuntu only (got ${PRETTY_NAME:-unknown})"
log "host: $(hostname) — ${PRETTY_NAME} — $(uname -m) — $(nproc) cores — $(free -g | awk '/Mem/{print $2}') GB RAM"

SUDO=""
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null || die "sudo required for apt"
  sudo -n true 2>/dev/null || die "sudo must be passwordless for an unattended bootstrap"
  SUDO="sudo -n"
fi

# ------------------------------------------------------------------- apt packages

# Host tooling that is not a KiCad build dependency.
HOST_PKGS=(
  git gh rsync jq unzip tmux htop curl ca-certificates
  build-essential cmake ninja-build ccache pkg-config gettext
  python3 python3-dev python3-venv python3-pip
  default-jre-headless
)

# KiCad 10 build dependencies, named explicitly so a missing one is a clear apt error rather
# than a cryptic CMake "Could NOT find" two minutes into configure. The list was completed by
# running `phicad configure dev` on Debian 13 until it passed (2026-09-22): the three
# entries marked (configure) were each the next "Could NOT find" of that loop.
KICAD_PKGS=(
  swig
  # wx-config advertises webview/media even when only the core -dev package is installed, and
  # FindwxWidgets then fails on the missing library file; KiCad links webview. (configure)
  libwxgtk3.2-dev libwxgtk-webview3.2-dev libwxgtk-media3.2-dev
  libspnav-dev            # 3Dconnexion SpaceMouse: find_package(SPNAV) is REQUIRED on Linux (configure)
  libpoppler-glib-dev libpoppler-private-dev   # PDF import (find_package(Poppler ... Glib)) (configure)
  libboost-dev libboost-filesystem-dev libboost-system-dev libboost-locale-dev libboost-test-dev
  libglew-dev libglm-dev libgl-dev
  libcairo2-dev libpixman-1-dev
  libcurl4-openssl-dev libssl-dev
  libocct-foundation-dev libocct-modeling-data-dev libocct-modeling-algorithms-dev
  libocct-data-exchange-dev libocct-ocaf-dev libocct-visualization-dev
  libngspice0-dev
  libprotobuf-dev protobuf-compiler
  libnng-dev
  libgit2-dev
  libzstd-dev zlib1g-dev
  libharfbuzz-dev libfreetype-dev libfontconfig-dev
  libgtk-3-dev
  unixodbc-dev
  libsecret-1-dev
)

missing=()
for p in "${HOST_PKGS[@]}" "${KICAD_PKGS[@]}"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
done

if [[ ${#missing[@]} -gt 0 ]]; then
  log "apt: installing ${#missing[@]} packages: ${missing[*]}"
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq --no-install-recommends "${missing[@]}"
else
  log "apt: all ${#HOST_PKGS[@]} host + ${#KICAD_PKGS[@]} KiCad packages already installed"
fi

# Debian's kicad source package knows the long tail (wxPython, i18n tools, test deps) but also
# pulls the documentation toolchain (TeX Live, ~1.5 GB) that a build host never needs. The
# explicit list above is what `phicad doctor` verifies; treat build-dep as the fallback when
# configure still reports a missing library. Only meaningful when deb-src lines exist.
if [[ "${PHICAD_BOOTSTRAP_BUILD_DEP:-0}" == 1 ]]; then
  if grep -rqsE '^\s*deb-src|^\s*Types:.*deb-src' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
    if [[ ! -f /var/lib/phicad-build-dep-kicad.done ]]; then
      log "apt: build-dep kicad (Debian's own dependency list, long tail)"
      $SUDO apt-get build-dep -y -qq kicad && $SUDO touch /var/lib/phicad-build-dep-kicad.done
    else
      log "apt: build-dep kicad already applied"
    fi
  else
    warn "no deb-src entries — skipping 'apt-get build-dep kicad'"
  fi
else
  log "apt: skipping 'build-dep kicad' (set PHICAD_BOOTSTRAP_BUILD_DEP=1 if configure misses a library)"
fi

# ------------------------------------------------------------------- user tooling

# ccache: the fork is ~3400 targets; a machine-wide cache makes worktree rebuilds cheap.
if command -v ccache >/dev/null; then
  ccache --set-config=max_size=20G >/dev/null 2>&1 || true
  log "ccache: $(ccache --version | head -1), max_size=$(ccache --get-config max_size 2>/dev/null || echo '?')"
fi

# Optional user-local tools (the AI features use phi, an agent harness; the web views use node).
# Report, do not install: their versions are owned by their own release channels.
for t in node npm phi; do
  if command -v "$t" >/dev/null; then
    printf '  %-6s %s (%s)\n' "$t" "$(command -v "$t")" "$("$t" --version 2>/dev/null | head -1)"
  else
    warn "$t not on PATH (optional; only needed for the AI / web features)"
  fi
done

# ------------------------------------------------------------------- summary

log "toolchain:"
for t in gcc g++ cmake ninja ccache swig protoc java gh python3; do
  printf '  %-8s %s\n' "$t" "$("$t" --version 2>&1 | head -1 || echo MISSING)"
done
printf '  %-8s %s\n' "wx" "$(wx-config --version 2>/dev/null || echo MISSING)"
printf '  %-8s %s\n' "nng" "$(pkg-config --modversion nng 2>/dev/null || echo MISSING)"
printf '  %-8s %s\n' "occt" "$(ls -d /usr/include/opencascade 2>/dev/null || echo MISSING)"
printf '  %-8s %s\n' "ngspice" "$(ls /usr/include/ngspice/sharedspice.h 2>/dev/null || echo MISSING)"
log "bootstrap complete — next: clone the fork, then 'phicad/bin/phicad configure' (Linux preset)"
