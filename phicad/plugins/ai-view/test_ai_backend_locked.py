#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Copyright (C) 2026 Exowatt, Inc. / KiCad Developers.
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
# along with this program; if not, see <http://www.gnu.org/licenses/>.

"""phi is the only harness, and no environment variable can move it.

Phase 1 ships this application to other people, so "the assistant talks to phi" has to be a
property of the build rather than of the machine it happens to run on. These tests assert the
NEGATIVE -- that the knobs which used to steer the assistant no longer do -- which is the part a
reader cannot verify by inspection, because absence of behaviour looks identical to untested code.

Why the negative matters more than the positive here: an env var that redirects the AI backend is
reachable by anything that touches the launching environment (a shell profile, a stale Finder
session, a `.env` in the project directory, another tool's wrapper). Whatever it points at
receives the board context and the user's prompts, and -- for the gateway variables -- the bearer
token meant for the configured endpoint. So each variable gets a test that sets it to something
obviously wrong and proves the code ignored it.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "pylib"))

import chat_backend                                                    # noqa: E402
# Tests run against a configured (fake) gateway; the unset case is tested explicitly below.
os.environ.setdefault("PHICAD_LLM_GATEWAY", "https://llm-gateway.example")
import gateway_models                                                  # noqa: E402


def _fake_cli(directory: pathlib.Path, name: str) -> pathlib.Path:
    """An executable that would be catastrophic to actually select."""
    p = directory / name
    p.write_text("#!/bin/sh\necho WRONG_BACKEND_RAN\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


class BackendIsLocked(unittest.TestCase):
    """resolve_backend() consults phi and nothing else."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in
                       ("PHICAD_AI_CMD", "PHICAD_AI_BACKEND", "PHICAD_AI_MODEL",
                        "PHICAD_CODEX_HOME", "PATH")}
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _resolved_exe(self):
        b = chat_backend.resolve_backend()
        return None if b is None else pathlib.Path(b.argv[0]).name

    def test_ai_cmd_override_is_ignored(self):
        """$PHICAD_AI_CMD pointed at another binary must not be honoured."""
        rogue = _fake_cli(self.dir, "rogue-llm")
        os.environ["PHICAD_AI_CMD"] = str(rogue)
        exe = self._resolved_exe()
        self.assertNotEqual(exe, "rogue-llm",
                            "PHICAD_AI_CMD still selects an arbitrary binary")
        self.assertIn(exe, (None, "phi"), f"resolved something unexpected: {exe}")

    def test_ai_backend_codex_is_ignored_even_when_codex_exists(self):
        """Asking for codex, with codex genuinely on PATH, still yields phi or nothing."""
        _fake_cli(self.dir, "codex")
        os.environ["PATH"] = f"{self.dir}:{os.environ.get('PATH', '')}"
        os.environ["PHICAD_AI_BACKEND"] = "codex"
        self.assertIn(self._resolved_exe(), (None, "phi"),
                      "PHICAD_AI_BACKEND=codex still reaches the Codex CLI")

    def test_no_silent_codex_fallback_when_phi_is_absent(self):
        """A machine with codex but no phi reports nothing rather than substituting.

        The old behaviour fell back to codex 'so the panel still works'. That trades a clear
        error for a different vendor, a different credential and unbudgeted spend -- decided
        by which binaries happen to be installed.
        """
        _fake_cli(self.dir, "codex")
        os.environ["PATH"] = str(self.dir)          # codex present, phi absent
        b = chat_backend.resolve_backend()
        if b is not None:                            # only ~/.local/bin/phi could satisfy this
            self.assertEqual(pathlib.Path(b.argv[0]).name, "phi")

    def test_model_pin_is_ignored(self):
        """$PHICAD_AI_MODEL must not pin a model; the live phi session owns it."""
        os.environ["PHICAD_AI_MODEL"] = "definitely-not-a-model"
        b = chat_backend.resolve_backend()
        if b is not None:
            self.assertNotEqual(b.model, "definitely-not-a-model",
                                "PHICAD_AI_MODEL still overrides the session's model")

    def test_resolve_backend_takes_no_environment_input(self):
        """Structural: the function body reads no os.environ at all."""
        import inspect
        src = inspect.getsource(chat_backend.resolve_backend)
        body = src.split('"""')[-1]                  # past the docstring
        self.assertNotIn("environ", body,
                         "resolve_backend() reads the environment again")

    def test_codex_is_unreachable_from_resolution(self):
        """_find_codex survives for its dialect, but nothing selects it."""
        import inspect
        src = inspect.getsource(chat_backend.resolve_backend)
        self.assertNotIn("_find_codex", src,
                         "resolve_backend() can still reach the Codex finder")

    def test_old_overrides_do_not_reach_the_phi_child(self):
        for key in chat_backend.IGNORED_AI_ENV:
            os.environ[key] = "attacker-controlled-value"
        child = chat_backend.child_env()
        for key in chat_backend.IGNORED_AI_ENV:
            self.assertNotIn(key, child, f"{key} leaked into the phi child")

    def test_ignored_overrides_are_reported_not_silent(self):
        """A user who set an inert knob is told it did nothing."""
        self.assertEqual(chat_backend.ignored_ai_overrides({}), [])
        got = chat_backend.ignored_ai_overrides(
            {"PHICAD_AI_MODEL": "x", "PHICAD_AI_CMD": " ", "PHICAD_AI_BACKEND": "codex"})
        self.assertIn("PHICAD_AI_MODEL", got)
        self.assertIn("PHICAD_AI_BACKEND", got)
        self.assertNotIn("PHICAD_AI_CMD", got, "whitespace-only should not count as set")


class GatewayIsLocked(unittest.TestCase):
    """The gateway is fixed and the credential crosses only phi's public pipe contract."""

    TOKEN = "sk-test-never-print-this"

    @classmethod
    def _payload(cls, **changes):
        payload = {
            "schemaVersion": 1,
            "token": cls.TOKEN,
            "provider": "gateway",
            "baseUrl": "https://llm-gateway.example",
            "expiresAt": None,
        }
        payload.update(changes)
        return json.dumps(payload)

    def test_gateway_comes_from_the_environment(self):
        self.assertEqual(gateway_models.GATEWAY, "https://llm-gateway.example")

    def test_redirected_gateway_never_receives_the_key(self):
        """Pointing PHICAD_LLM_GATEWAY elsewhere cannot hand that host the user's key.

        The key is only released when phi reports the SAME provider and baseUrl that the
        environment names, so a redirect to an attacker fails closed before any request.
        """
        import importlib
        saved = os.environ.get("PHICAD_LLM_GATEWAY")
        os.environ["PHICAD_LLM_GATEWAY"] = "https://attacker.example/v1"
        try:
            importlib.reload(gateway_models)
            self.assertEqual(gateway_models.GATEWAY, "https://attacker.example/v1")

            def run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=self._payload(), stderr="")

            with self.assertRaises(RuntimeError) as ctx:
                gateway_models._api_key("/fake/phi", runner=run)
            self.assertIn("unsupported credential contract", str(ctx.exception))
            self.assertNotIn(self.TOKEN, str(ctx.exception))
        finally:
            if saved is None:
                os.environ.pop("PHICAD_LLM_GATEWAY", None)
            else:
                os.environ["PHICAD_LLM_GATEWAY"] = saved
            importlib.reload(gateway_models)

    def test_unset_gateway_disables_live_models(self):
        """With no gateway configured nothing is contacted and phi is never asked for a key."""
        import importlib
        saved = os.environ.pop("PHICAD_LLM_GATEWAY", None)
        try:
            importlib.reload(gateway_models)
            self.assertEqual(gateway_models.GATEWAY, "")

            def run(argv, **kwargs):
                raise AssertionError("phi must not be asked for a key without a gateway")

            with self.assertRaises(RuntimeError) as ctx:
                gateway_models._api_key("/fake/phi", runner=run)
            self.assertIn("PHICAD_LLM_GATEWAY", str(ctx.exception))
        finally:
            if saved is not None:
                os.environ["PHICAD_LLM_GATEWAY"] = saved
            importlib.reload(gateway_models)

    def test_auth_file_env_override_is_inert(self):
        """The old path override cannot revive a direct credential-file read."""
        import importlib
        saved = os.environ.get("PHICAD_LLM_AUTH_FILE")
        os.environ["PHICAD_LLM_AUTH_FILE"] = "/tmp/not-the-real-store.json"
        try:
            importlib.reload(gateway_models)
            self.assertFalse(hasattr(gateway_models, "AUTH_FILE"),
                             "gateway_models still exposes a credential-file path")
        finally:
            if saved is None:
                os.environ.pop("PHICAD_LLM_AUTH_FILE", None)
            else:
                os.environ["PHICAD_LLM_AUTH_FILE"] = saved
            importlib.reload(gateway_models)

    def test_credential_uses_exact_phi_contract(self):
        """The key enters through stdout from one fixed argv, never through argv itself."""
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, self._payload(), "")

        self.assertEqual(gateway_models._api_key("/fixed/phi", runner=run), self.TOKEN)
        self.assertEqual(calls[0][0], ["/fixed/phi", "auth", "token", "--json"])
        self.assertNotIn(self.TOKEN, calls[0][0])
        self.assertTrue(calls[0][1]["capture_output"])
        self.assertFalse(calls[0][1]["check"])

    def test_contract_rejects_wrong_provider_or_endpoint(self):
        for change in ({"provider": "openai"}, {"baseUrl": "https://attacker.example"},
                       {"schemaVersion": 2}, {"token": ""}):
            def run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, self._payload(**change), "")
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                gateway_models._api_key("/fixed/phi", runner=run)

    def test_failure_never_echoes_broker_output(self):
        """Even a hostile broker cannot inject a key into diagnostics."""
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2, self.TOKEN, self.TOKEN)
        with self.assertRaises(RuntimeError) as raised:
            gateway_models._api_key("/fixed/phi", runner=run)
        self.assertNotIn(self.TOKEN, str(raised.exception))
        self.assertIn("not signed in", str(raised.exception))

    def test_old_phi_has_one_actionable_failure(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "unknown auth command")
        with self.assertRaises(RuntimeError) as raised:
            gateway_models._api_key("/fixed/phi", runner=run)
        self.assertIn("phi update self", str(raised.exception))
        self.assertIn(gateway_models.PHI_AUTH_TOKEN_MIN_VERSION, str(raised.exception))

    def test_direct_gateway_request_has_person_key_and_app_tag(self):
        """Identity comes from phi; app attribution is the separate LiteLLM tag."""
        captured = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def urlopen(request, timeout):
            captured.append(request)
            return Response(b'{"data": []}')

        with mock.patch.object(gateway_models, "_api_key", return_value=self.TOKEN), \
                mock.patch.object(gateway_models.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(gateway_models.fetch_model_groups(), {})
        self.assertEqual(captured[0].get_header("Authorization"), f"Bearer {self.TOKEN}")
        self.assertEqual(captured[0].get_header("X-litellm-tags"), "client=phicad")

    def test_ignored_gateway_overrides_are_reported(self):
        self.assertEqual(gateway_models.ignored_gateway_overrides({}), [])
        self.assertEqual(
            gateway_models.ignored_gateway_overrides({"PHICAD_LLM_AUTH_FILE": "x", "PHICAD_LLM_GATEWAY": "x"}),
            ["PHICAD_LLM_AUTH_FILE"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
