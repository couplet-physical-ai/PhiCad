#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Host-terminal session bridge: not included in this build (feature gated off).

PhiCad's internal builds can share one live phi conversation between Agent View and an
agent-host terminal.  That bridge depends on a host application that is not published, so
this build ships this inert module instead: no project is ever "connected", every connect
attempt fails with a clear :class:`BridgeError`, and Agent View runs standalone through
local phi.  The public names are kept so the callers need no conditional imports.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import project_history

AVAILABLE = False
DESCRIPTOR_NAME = project_history.BRIDGE_NAME
_DISABLED = "the host-terminal session bridge is not included in this build; Agent View uses local phi"


class BridgeError(RuntimeError):
    """A bridge operation was requested; always raised in this build."""


@dataclass(frozen=True)
class SessionMessage:
    entry_id: str
    role: str
    text: str
    timestamp: str = ""


@dataclass(frozen=True)
class BridgeDescriptor:
    project_dir: Path
    session_id: str
    session_file: Path
    terminal_handle: str
    cli: str
    worktree: str = ""
    tab_id: str = ""
    provider: str = ""
    model: str = ""


def project_dir(path: Path | str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_file() or candidate.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_sch"):
        return candidate.parent
    return candidate


def descriptor_path(path: Path | str) -> Path:
    return project_dir(path) / project_history.CONFIG_DIR / DESCRIPTOR_NAME


def connect(path: Path | str, env: Optional[dict[str, str]] = None, **_kwargs) -> BridgeDescriptor:
    raise BridgeError(_DISABLED)


def disconnect(path: Path | str) -> bool:
    """Nothing is ever connected, so there is nothing to remove."""
    return False


def load(path: Path | str) -> Optional[BridgeDescriptor]:
    """Always ``None``: no project is bound to a host session in this build."""
    return None


def session_messages(path: Path | str) -> list[SessionMessage]:
    return []


def model_report(descriptor: BridgeDescriptor) -> dict:
    return {}


class HostChatSession:
    """Placeholder for the shared-session chat; constructing it always fails."""

    def __init__(self, descriptor: BridgeDescriptor, *args, **kwargs) -> None:
        raise BridgeError(_DISABLED)


def main(argv: Optional[Sequence[str]] = None) -> int:
    print(_DISABLED, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
