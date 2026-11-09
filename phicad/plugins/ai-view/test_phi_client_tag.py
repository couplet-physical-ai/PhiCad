#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Exowatt, Inc. / KiCad Developers.

"""Execute the explicit phi extension that attributes Agent View gateway spend.

This is intentionally a runtime test, not a source-text assertion. Node imports the same ESM
file that phi loads, a fake ExtensionAPI captures its registered hook, and the hook mutates real
header objects. A typo in the event name or an unexportable module therefore fails the gate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import chat_backend

EXTENSION = Path(__file__).with_name("phi_client_tag.mjs")
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required to execute the bundled phi extension")
class PhiClientTagTests(unittest.TestCase):
    def _node(self, body: str):
        script = f"""
import register, {{ addPhiCadClientTag, CLIENT_TAG }} from {json.dumps(EXTENSION.as_uri())};
{body}
"""
        result = subprocess.run(
            [NODE, "--input-type=module", "--eval", script],
            capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_turn_loads_exactly_the_bundled_attribution_extension(self):
        backend = chat_backend.Backend(argv=["/bin/phi"], source="test")
        argv = chat_backend.turn_argv(
            backend, session_ref="s", system_prompt="system", prompt="prompt")
        self.assertIn("--no-extensions", argv)
        pos = argv.index("--extension")
        self.assertEqual(Path(argv[pos + 1]), EXTENSION.resolve())
        self.assertEqual(argv.count("--extension"), 1)
        self.assertNotIn("sk-", " ".join(argv))

    def test_extension_registers_only_the_header_hook(self):
        got = self._node("""
const hooks = [];
register({ on(name, handler) { hooks.push([name, typeof handler]); } });
console.log(JSON.stringify({ hooks, tag: CLIENT_TAG }));
""")
        self.assertEqual(got, {
            "hooks": [["before_provider_headers", "function"]],
            "tag": "client=phicad",
        })

    def test_hook_adds_tag_and_preserves_existing_tags(self):
        got = self._node("""
let hook;
register({ on(name, handler) { hook = handler; } });
const empty = {};
hook({ headers: empty }, {});
const existing = { "X-LiteLLM-Tags": "team=hardware,production" };
hook({ headers: existing }, {});
hook({ headers: existing }, {});
console.log(JSON.stringify({ empty, existing }));
""")
        self.assertEqual(got["empty"]["x-litellm-tags"], "client=phicad")
        self.assertEqual(
            got["existing"]["x-litellm-tags"],
            "team=hardware,production,client=phicad")
        self.assertIsNone(got["existing"]["X-LiteLLM-Tags"])
        self.assertEqual(got["existing"]["x-litellm-tags"].count("client=phicad"), 1)

    def test_exported_helper_handles_a_plain_header_object(self):
        got = self._node("""
const headers = { "x-litellm-tags": "client=phicad,client=phicad" };
addPhiCadClientTag(headers);
console.log(JSON.stringify(headers));
""")
        self.assertEqual(got["x-litellm-tags"], "client=phicad")


if __name__ == "__main__":
    unittest.main(verbosity=2)
