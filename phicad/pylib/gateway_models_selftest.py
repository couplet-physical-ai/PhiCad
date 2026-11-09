#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""CI-safe checks for gateway_models: everything that needs no gateway.

TAP-ish output like telemetry_selftest.py.  Network paths (fetch_model_groups)
are deliberately untested here; `phicad models check` exercises them live.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import os  # noqa: E402
os.environ.setdefault("PHICAD_LLM_GATEWAY", "https://llm-gateway.example")  # a configured (fake) gateway
import gateway_models  # noqa: E402

CHECKS = 0


def ok(cond: bool, name: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"not ok {CHECKS} - {name}")
        sys.exit(1)
    print(f"ok {CHECKS} - {name}")


def agents_dir(root: Path) -> Path:
    d = root / "phicad" / "agents"
    d.mkdir(parents=True)
    return d


def main() -> int:
    # --- parse_ref ------------------------------------------------------------
    ok(gateway_models.parse_ref("gateway/fast:max") == ("gateway", "fast", "max"),
       "parse_ref: provider/alias:suffix")
    ok(gateway_models.parse_ref("fast") == ("", "fast", None),
       "parse_ref: bare alias")
    ok(gateway_models.parse_ref("gateway/bulk") == ("gateway", "bulk", None),
       "parse_ref: provider, no suffix")

    # --- check: local rules ----------------------------------------------------
    refs = {
        "good.md": "gateway/fast:max",
        "bad-suffix.md": "gateway/strong:enormous",
        "bulk-suffixed.md": "gateway/bulk:high",
    }
    offline = gateway_models.check(refs, None)
    ok(len(offline) == 2, "offline check: exactly the two local-rule violations")
    ok(any("bulk-suffixed" in p and "no reasoning" in p for p in offline),
       "offline check: no-reasoning rule names the file")
    ok(not any("good.md" in p for p in offline), "offline check: sound ref passes")
    ok(not any("is not served" in p for p in offline),
       "offline check: alias existence is never judged without the gateway")

    # --- check: gateway rules --------------------------------------------------
    groups = {"fast": {}, "strong": {}, "bulk": {}}
    online = gateway_models.check({"gone.md": "gateway/retired6:max"}, groups)
    ok(any("not served" in p for p in online), "online check: unknown alias fails")

    # --- retarget ----------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        d = agents_dir(root)
        (d / "router.md").write_text("---\nname: r\nmodel: gateway/fast:max\n---\nbody\n")
        (d / "scout.md").write_text("---\nname: s\nmodel: gateway/bulk\n---\nbody\n")
        (d / "README.md").write_text("model: gateway/fast:max\n")

        changes = gateway_models.retarget({"fast": "fast-6"}, root=root, dry_run=True)
        ok(changes == ["router.md: gateway/fast:max -> gateway/fast-6:max"],
           "retarget dry-run: provider and suffix preserved, only the alias moves")
        ok("model: gateway/fast:max" in (d / "router.md").read_text(),
           "retarget dry-run: nothing written")

        gateway_models.retarget({"fast": "fast-6"}, root=root)
        ok("model: gateway/fast-6:max" in (d / "router.md").read_text(),
           "retarget: frontmatter rewritten in place")
        ok("model: gateway/bulk" in (d / "scout.md").read_text(),
           "retarget: unmapped aliases untouched")
        ok("model: gateway/fast:max" in (d / "README.md").read_text(),
           "retarget: README is never rewritten")

        ok(gateway_models.agent_refs(root) == {"router.md": "gateway/fast-6:max",
                                               "scout.md": "gateway/bulk"},
           "agent_refs: reads frontmatter, skips README")

    # --- phi's public credential-handoff contract -------------------------------
    token = "sk-fixture-must-not-reach-diagnostics"
    payload = json.dumps({
        "schemaVersion": 1,
        "token": token,
        "provider": "gateway",
        "baseUrl": gateway_models.GATEWAY,
        "expiresAt": None,
    })
    calls = []

    def successful_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, payload, "")

    ok(gateway_models._api_key("/fixture/phi", runner=successful_runner) == token,
       "phi auth: schema-v1 token accepted")
    ok(calls[0][0] == ["/fixture/phi", "auth", "token", "--json"],
       "phi auth: exact public command contract")
    ok(token not in calls[0][0], "phi auth: token never enters argv")

    def failed_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, token, token)

    try:
        gateway_models._api_key("/fixture/phi", runner=failed_runner)
        failed = "no error"
    except RuntimeError as exc:
        failed = str(exc)
    ok("not signed in" in failed, "phi auth: stable exit 2 is actionable")
    ok(token not in failed, "phi auth: stdout/stderr never reach diagnostics")

    # --- the real repo's refs pass the offline rules ----------------------------
    ok(gateway_models.check(gateway_models.agent_refs(), None) == [],
       "repo agents: offline rules hold for every checked-in ref")

    print(f"gateway_models selftest: all {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
