#!/usr/bin/env python3
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

"""An explicitly authorized, non-renewing host run window. No credentials are read."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import time
from pathlib import Path


def deadline(home: Path, now: float | None = None) -> float | None:
    path = home / "control/run-window.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    end = dt.datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    start = dt.datetime.strptime(data["authorized_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    current = time.time() if now is None else now
    if data.get("mode") != "uncapped" or not 0 < end.timestamp() - start.timestamp() <= 21600:
        raise ValueError("invalid run-window authorization")
    if not start.timestamp() <= current < end.timestamp():
        raise ValueError("run-window authorization is not active")
    return end.timestamp()


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def expire(home: Path) -> None:
    # A persistent pause blocks both scheduled and manual `once --item` activations.
    (home / "control/PAUSE").touch(mode=0o600)
    systemctl("stop", "phicad-loop.timer")
    # KillMode=control-group includes child processes that started new Unix sessions.
    # SIGKILL is immediate at the absolute cutoff: all disk work remains in wt/.
    subprocess.run(["systemctl", "--user", "kill", "--kill-whom=all", "--signal=SIGKILL",
                    "phicad-loop.service"], check=False)
    systemctl("stop", "phicad-loop.service")


def install(home: Path, authorized_at: str, expires_at: str) -> None:
    control = home / "control"
    control.mkdir(parents=True, exist_ok=True)
    path = control / "run-window.json"
    if path.exists():
        raise ValueError("a run window already exists; never renew one implicitly")
    # Install while paused; this command never starts a paid iteration.
    (control / "PAUSE").touch(mode=0o600)
    systemctl("stop", "phicad-loop.timer")
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "phicad-loop.service"])
    if active.returncode == 0:
        raise ValueError("stop the active iteration before installing a window")
    data = {"authorized_at": authorized_at, "expires_at": expires_at, "mode": "uncapped"}
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    try:
        deadline(home)
    except Exception:
        path.unlink()
        raise
    pinned = control / "window-runner"
    pinned.mkdir(exist_ok=True)
    for name in ("loop.py", "run_window.py"):
        shutil.copy2(Path(__file__).parent / name, pinned / name)
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    dropin = units / "phicad-loop.service.d"
    dropin.mkdir(exist_ok=True)
    # %h is expanded by systemd, never by a shell. The environment/key source is unchanged.
    script = "%h/phicad-loop/control/window-runner/run_window.py"
    (dropin / "90-authorized-window.conf").write_text(
        "[Service]\nExecCondition=/usr/bin/python3 " + script + " guard\n"
        "ExecStart=\nExecStart=/usr/bin/python3 %h/phicad-loop/control/window-runner/loop.py once\n"
        "KillMode=control-group\nSendSIGKILL=yes\nTimeoutStopSec=1s\n")
    (units / "phicad-loop-expiry.service").write_text(
        "[Unit]\nDescription=Expire the explicitly authorized PhiCad run window\n"
        "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 " + script + " expire\n")
    calendar = expires_at.replace("T", " ").replace("Z", " UTC")
    (units / "phicad-loop-expiry.timer").write_text(
        "[Unit]\nDescription=Absolute PhiCad authorization expiry\n[Timer]\n"
        "OnCalendar=" + calendar + "\nAccuracySec=1us\nRandomizedDelaySec=0\nPersistent=true\n"
        "Unit=phicad-loop-expiry.service\n[Install]\nWantedBy=timers.target\n")
    systemctl("daemon-reload")
    systemctl("enable", "--now", "phicad-loop-expiry.timer")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("install", "guard", "expire"))
    ap.add_argument("--authorized-at")
    ap.add_argument("--expires-at")
    args = ap.parse_args()
    home = Path.home() / "phicad-loop"
    try:
        if args.action == "install":
            if not args.authorized_at or not args.expires_at:
                raise ValueError("both absolute timestamps are required")
            install(home, args.authorized_at, args.expires_at)
        elif args.action == "guard":
            if deadline(home) is None:
                raise ValueError("run-window authorization missing")
        else:
            expire(home)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
