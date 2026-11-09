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
"""Guard the published integration contracts and measurement records against drift.

The schemas under `phicad/integration`, `phicad/synth`, `phicad/measurements`, and
`phicad/support` are machine contracts.  Nothing checked them, so a typo in a schema, or a document
that no longer satisfies the schema it names, would ship silently and only surface as a puzzling
host-side rejection.

Runs stdlib-only so it works in `phicad check fast` on a bare checkout with no venv:

  1. every schema parses, declares $schema/$id/title, and has a sane top-level shape;
  2. every `required` name actually appears in `properties` -- the mistake that makes a
     schema impossible to satisfy, and the one a human reviewer is least likely to spot;
  3. shipped design-IR examples and milestone measurements validate against their schemas.

Step 3 needs a real validator.  When `jsonschema` is importable (either venv has it) the
instances are validated properly; otherwise the check degrades to the structural subset and
says so, because a gate must never claim more than it verified.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

DESIGN_SCHEMA = REPO / "phicad/synth/design-ir.schema.json"
MEASUREMENT_SCHEMA = REPO / "phicad/measurements/measurement.schema.json"
FEEDBACK_SCHEMA = REPO / "phicad/support/feedback-schema.json"

SCHEMAS = [
    REPO / "phicad/integration/design-workflow-event.schema.json",
    REPO / "phicad/integration/host-action.schema.json",
    DESIGN_SCHEMA,
    MEASUREMENT_SCHEMA,
    FEEDBACK_SCHEMA,
]

# (instance, schema it must satisfy).  Measurements are discovered so adding a record cannot
# silently bypass the gate by forgetting to register its filename here.
INSTANCES = [
    (REPO / "phicad/synth/examples/sensor-node.json", DESIGN_SCHEMA),
    (REPO / "phicad/synth/examples/regulated-breakout.json", DESIGN_SCHEMA),
    (REPO / "phicad/support/feedback-example.json", FEEDBACK_SCHEMA),
]
INSTANCES += [(path, MEASUREMENT_SCHEMA)
              for path in sorted(MEASUREMENT_SCHEMA.parent.glob("*.json"))
              if not path.name.endswith(".schema.json")]


def structural_problems(path: Path, schema: dict) -> list[str]:
    rel = path.relative_to(REPO)
    problems = []
    for key in ("$schema", "$id", "title"):
        if key not in schema:
            problems.append(f"{rel}: missing {key}")

    def walk(node: object, where: str) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        required = node.get("required")
        if isinstance(required, list) and isinstance(properties, dict):
            for name in required:
                # additionalProperties/patternProperties can legitimately supply a required
                # name, so only flag it when nothing else could.
                if name not in properties and not node.get("patternProperties"):
                    problems.append(f"{rel}: {where}required '{name}' is not in properties")
        for key, value in node.items():
            if key in ("properties", "definitions", "$defs") and isinstance(value, dict):
                for name, child in value.items():
                    walk(child, f"{where}{key}.{name}: ")
            elif key in ("items", "if", "then", "else", "not", "additionalProperties"):
                walk(value, f"{where}{key}: ")
            elif key in ("oneOf", "anyOf", "allOf") and isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{where}{key}[{index}]: ")

    walk(schema, "")
    return problems


def main() -> int:
    problems: list[str] = []
    loaded: dict[Path, dict] = {}

    for path in SCHEMAS:
        if not path.is_file():
            problems.append(f"{path.relative_to(REPO)}: missing")
            continue
        try:
            schema = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"{path.relative_to(REPO)}: invalid JSON: {exc}")
            continue
        loaded[path] = schema
        problems += structural_problems(path, schema)

    try:
        import jsonschema
    except ImportError:
        jsonschema = None

    validated = 0
    for instance_path, schema_path in INSTANCES:
        if not instance_path.is_file():
            problems.append(f"{instance_path.relative_to(REPO)}: missing")
            continue
        try:
            instance = json.loads(instance_path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"{instance_path.relative_to(REPO)}: invalid JSON: {exc}")
            continue
        schema = loaded.get(schema_path)
        if schema is None or jsonschema is None:
            continue
        try:
            jsonschema.validate(instance, schema, format_checker=jsonschema.FormatChecker())
            validated += 1
        except jsonschema.ValidationError as exc:
            location = "/".join(str(part) for part in exc.absolute_path) or "(root)"
            problems.append(f"{instance_path.relative_to(REPO)}: at {location}: {exc.message}")
        except jsonschema.SchemaError as exc:
            problems.append(f"{schema_path.relative_to(REPO)}: schema itself is invalid: {exc.message}")

    if problems:
        for problem in problems:
            print(problem)
        print(f"\n{len(problems)} schema problem(s)")
        return 1

    if jsonschema is None:
        print(f"schemas ok: {len(loaded)} parsed + structurally checked; "
              f"instances NOT validated (no jsonschema module — run under an MCP/automation venv)")
    else:
        print(f"schemas ok: {len(loaded)} parsed + structurally checked, "
              f"{validated} instance(s) validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
