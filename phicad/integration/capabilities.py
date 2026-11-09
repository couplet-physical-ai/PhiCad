#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, you may find one here:
# http://www.gnu.org/licenses/gpl-2.0.html
#
"""Discoverable handshake shared by the CLI, MCP, agent hosts, and future model hosts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def describe(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    event_schema = repo_root / "phicad" / "integration" / "design-workflow-event.schema.json"
    action_schema = repo_root / "phicad" / "integration" / "host-action.schema.json"
    design_schema = repo_root / "phicad" / "synth" / "design-ir.schema.json"
    missing = [str(path) for path in (event_schema, action_schema, design_schema)
               if not path.is_file()]
    return {
        "ok": not missing,
        "protocol_version": 1,
        "command_plane": {"transport": "mcp", "version": "negotiated"},
        "workflow": {
            "event_schema": "phicad.design-workflow.event/v1",
            "action_schema": "phicad.host-action/v1",
            "implemented_transports": ["in-process"],
            "planned_transports": ["authenticated-loopback-sse"],
            "human_approval_required_before_build": True,
        },
        "model_adapter": {
            "protocol_version": 1,
            "negotiates_capabilities": [
                "structured_output",
                "tool_calls",
                "streaming",
                "vision",
                "background_tasks",
                "session_continuity",
            ],
            "design_schema_id": "https://couplet-physical-ai.github.io/PhiCad/schemas/design-ir.v1.json",
        },
        "schemas": {
            "workflow_event": str(event_schema),
            "host_action": str(action_schema),
            "design_ir": str(design_schema),
        },
        "repo_root": str(repo_root),
        "missing": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="phicad integration",
        description="Print PhiCad's host/model integration handshake as JSON.",
    )
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parents[2]),
        help="PhiCad source root (default: inferred from this file)",
    )
    args = parser.parse_args()
    result = describe(Path(args.repo))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
