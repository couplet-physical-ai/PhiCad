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
# install-remote.sh — put the PhiCad continuous development loop on a Linux host.
#
# Runs on the LAPTOP and drives the host over ssh (non-interactive `ssh <host> 'bash -s'`).
# Idempotent: re-run after a partial failure, or to change the branch/runner/interval.
#
#   phicad/loop/install-remote.sh <ssh-host> [--branch dev] [--runner phi] [--interval 30min]
#                                 [--kicad-cli PATH] [--build PATH] [--models 'tier=ref,...']
#                                 [--repo-url URL] [--no-timer] [--run-once]
#
# What it does on the host, as the ssh user:
#   1. ~/phicad-loop/{repo,wt,runs,outbox,control}  — the loop's OWN blobless clone of the fork
#      from --repo-url (default: the public repository); it never touches another checkout on
#      the box. Pushing loop branches needs a credential the host already has for that remote.
#   2. ~/phicad-loop/env (0600)                      — the loop's configuration, read by systemd.
#   3. systemd --user timer + service (phicad/loop/systemd/), `loginctl enable-linger` so the
#      timer survives logout (needs passwordless sudo; otherwise prints the command).
#   4. `loop.py validate` + `loop.py pick` as a smoke test; `--run-once` runs one real iteration
#      in the foreground and streams its output.
#
# Why --branch exists: the loop runs the loop.py and backlog found in its clone, so while this
# directory is still on a feature branch the host must track that branch; switch back to `dev`
# (the default) by re-running once it has merged.

set -euo pipefail

usage() { sed -n '20,41p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

HOST=""
BRANCH="dev"
RUNNER="phi"
INTERVAL="30min"
KICAD_CLI=""
BUILD=""
MODELS=""
TIMER=1
RUN_ONCE=0
REPO_URL="https://github.com/couplet-physical-ai/PhiCad.git"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH="$2"; shift 2 ;;
    --runner) RUNNER="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --kicad-cli) KICAD_CLI="$2"; shift 2 ;;
    --build) BUILD="$2"; shift 2 ;;
    --models) MODELS="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --no-timer) TIMER=0; shift ;;
    --run-once) RUN_ONCE=1; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown flag: $1" >&2; usage 2 ;;
    *) [[ -z "$HOST" ]] || { echo "one host only" >&2; usage 2; }; HOST="$1"; shift ;;
  esac
done
[[ -n "$HOST" ]] || usage 2
case "$RUNNER" in phi) ;; *) echo "--runner must be phi" >&2; exit 2 ;; esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The LLM proxy variables point at the LLM gateway and break ssh/git on the laptop.
ssh_() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"
}
log() { printf '\033[1;34m[install-remote]\033[0m %s\n' "$*" >&2; }

log "host $HOST · branch $BRANCH · runner $RUNNER · interval $INTERVAL"

# 1. preconditions ---------------------------------------------------------------------------
ssh_ 'bash -s' <<'EOF'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
fail=0
for tool in git python3 phi; do
  command -v "$tool" >/dev/null || { echo "missing on host: $tool" >&2; fail=1; }
done
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || {
  echo "host python3 is older than 3.11" >&2; fail=1; }
[[ $fail -eq 0 ]] || exit 3
echo "preconditions ok: $(uname -srm), $(python3 --version), phi $(phi --version 2>/dev/null | head -1)"
EOF

# 2. layout + clone --------------------------------------------------------------------------
ssh_ "REPO_URL='$REPO_URL' BRANCH='$BRANCH' bash -s" <<'EOF'
set -euo pipefail
umask 077
mkdir -p ~/phicad-loop/{wt,runs,outbox,control}
chmod 700 ~/phicad-loop
cd ~/phicad-loop
if [[ ! -d repo/.git ]]; then
  echo "cloning $REPO_URL (blob:none) -> ~/phicad-loop/repo"
  git clone -q --filter=blob:none --no-checkout "$REPO_URL" repo
fi
cd repo
git config user.name  "PhiCad loop ($(hostname -s))"
git config user.email "phicad-loop@$(hostname -s).invalid"
git config advice.detachedHead false
git fetch -q --prune origin
git checkout -q -B "$BRANCH" "origin/$BRANCH"
echo "repo at $(git rev-parse --short HEAD) on $BRANCH"
[[ -f phicad/loop/loop.py ]] || { echo "branch $BRANCH has no phicad/loop/loop.py" >&2; exit 4; }
EOF

# 3. environment file ------------------------------------------------------------------------
# Defaults for the shared build tree can be overridden; empty values are simply omitted.
ssh_ "BRANCH='$BRANCH' RUNNER='$RUNNER' KICAD_CLI='$KICAD_CLI' BUILD='$BUILD' MODELS='$MODELS' bash -s" <<'EOF'
set -euo pipefail
umask 077
{
  echo "# written by phicad/loop/install-remote.sh $(date -u +%FT%TZ)"
  echo "# edit by hand (the timer re-reads it every firing) or re-run the installer"
  echo "PHICAD_LOOP_HOME=$HOME/phicad-loop"
  echo "PHICAD_LOOP_BRANCH=$BRANCH"
  echo "PHICAD_LOOP_RUNNER=$RUNNER"
  echo "PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
  [[ -n "$KICAD_CLI" ]] && echo "PHICAD_KICAD_CLI=$KICAD_CLI"
  [[ -n "$BUILD" ]] && echo "PHICAD_BUILD=$BUILD"
  [[ -n "$MODELS" ]] && echo "PHICAD_LOOP_MODELS=$MODELS"
  echo "# spend caps (USD): one AI step / rolling 24 h; see phicad/loop/README.md"
  echo "#PHICAD_LOOP_MAX_COST_USD=25"
  echo "#PHICAD_LOOP_DAILY_CAP_USD=100"
  true
} > ~/phicad-loop/env
chmod 600 ~/phicad-loop/env
echo "env:"; sed 's/^/  /' ~/phicad-loop/env
EOF

# 4. systemd --user timer ---------------------------------------------------------------------
if [[ $TIMER -eq 1 ]]; then
  # units are shipped in the repo; the interval is the only per-host edit
  ssh_ "INTERVAL='$INTERVAL' bash -s" <<'EOF'
set -euo pipefail
mkdir -p ~/.config/systemd/user
sed "s/^OnUnitInactiveSec=.*/OnUnitInactiveSec=$INTERVAL/" \
  ~/phicad-loop/repo/phicad/loop/systemd/phicad-loop.timer > ~/.config/systemd/user/phicad-loop.timer
cp ~/phicad-loop/repo/phicad/loop/systemd/phicad-loop.service ~/.config/systemd/user/phicad-loop.service
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "no user systemd manager reachable (XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR)." >&2
  echo "  enable lingering first:  sudo loginctl enable-linger $USER   then re-run the installer" >&2
  exit 6
fi
systemctl --user daemon-reload
systemctl --user enable --now phicad-loop.timer >/dev/null
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  if sudo -n loginctl enable-linger "$USER" 2>/dev/null; then echo "linger enabled for $USER"
  else echo "NOTE: run once with sudo:  sudo loginctl enable-linger $USER   (timer would stop at logout)" >&2; fi
fi
systemctl --user list-timers phicad-loop.timer --no-pager | sed 's/^/  /'
EOF
fi

# 5. smoke -----------------------------------------------------------------------------------
ssh_ 'bash -s' <<'EOF'
set -euo pipefail
set -a; . ~/phicad-loop/env; set +a
cd ~/phicad-loop
python3 repo/phicad/loop/loop.py validate repo/phicad/loop/backlog
echo "next pick:"; python3 repo/phicad/loop/loop.py pick | sed 's/^/  /'
EOF

if [[ $RUN_ONCE -eq 1 ]]; then
  log "running one iteration in the foreground (this can take up to the item's budget)"
  ssh_ 'set -a; . ~/phicad-loop/env; set +a; cd ~/phicad-loop && nice -n 15 python3 repo/phicad/loop/loop.py once'
  ssh_ 'cat ~/phicad-loop/STATUS.md'
fi

log "done. Steer with: ssh $HOST 'touch ~/phicad-loop/control/PAUSE'   (rm to resume)"
log "      ship queued PRs from this laptop: python3 $HERE/loop.py ship-from $HOST"
