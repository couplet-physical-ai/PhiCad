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
"""The json-mode turn path: event parsing, usage honesty, and the text fallback.

Everything here runs headless against a FAKE phi executable written into a temp
directory — no network, no real model — because what is under test is the
CONTRACT, not the provider:

  * the reply is the assistant text from the event stream, nothing else;
  * usage fields are copied verbatim from the backend's own report and never
    invented (a backend that reports nothing yields usage None);
  * a CLI that rejects `--mode json` downgrades the backend ONCE and the turn
    is replayed in text mode, where only sizes are recorded.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import chat_backend


def _event(role: str = "assistant", texts=("OK",), model: str = "strong",
           provider: str = "gateway", usage: dict | None = None) -> str:
    message = {
        "role": role,
        "content": [{"type": "text", "text": t} for t in texts],
        "model": model,
        "provider": provider,
        "usage": usage if usage is not None else {
            "input": 12, "output": 7, "reasoning": 3,
            "cacheRead": 900, "cacheWrite": 100,
            "cost": {"input": 0.001, "output": 0.002, "total": 0.003},
        },
    }
    return json.dumps({"type": "message_end", "message": message})


class ParseEventsTests(unittest.TestCase):
    def test_extracts_text_and_verbatim_usage(self) -> None:
        stream = "\n".join([
            json.dumps({"type": "session", "model": "configured-not-actual"}),
            "not json at all",
            json.dumps({"type": "message_end", "message": {"role": "user"}}),
            _event(texts=("line one", "line two")),
        ])
        text, usage = chat_backend.parse_print_events(stream)
        self.assertEqual(text, "line one\nline two")
        assert usage is not None
        self.assertEqual((usage.model, usage.provider), ("strong", "gateway"))
        self.assertEqual((usage.tokens_in, usage.tokens_out, usage.tokens_reasoning),
                         (12, 7, 3))
        self.assertEqual(usage.cache_read, 900)
        self.assertEqual(usage.cost_usd, 0.003)

    def test_no_assistant_message_means_no_reply_and_no_usage(self) -> None:
        text, usage = chat_backend.parse_print_events(
            json.dumps({"type": "turn_end"}) + "\n")
        self.assertEqual(text, "")
        self.assertIsNone(usage)

    def test_missing_usage_fields_stay_none_never_zero(self) -> None:
        text, usage = chat_backend.parse_print_events(_event(usage={}))
        assert usage is not None
        self.assertIsNone(usage.tokens_in)
        self.assertIsNone(usage.cost_usd)
        # and telemetry_fields drops them rather than recording fabricated zeros
        recorded = usage.telemetry_fields()
        self.assertNotIn("tokens_in", recorded)
        self.assertNotIn("cost_usd", recorded)
        self.assertEqual(recorded.get("model"), "strong")

    def test_last_assistant_message_wins_for_identity(self) -> None:
        stream = _event(model="strong") + "\n" + _event(model="fast", texts=("second",))
        text, usage = chat_backend.parse_print_events(stream)
        self.assertIn("second", text)
        assert usage is not None
        self.assertEqual(usage.model, "fast")


class RejectedJsonModeTests(unittest.TestCase):
    def test_unknown_mode_flag_is_recognised(self) -> None:
        self.assertTrue(chat_backend._rejected_json_mode(
            2, "error: unknown option '--mode'"))

    def test_unrelated_failure_is_not_a_downgrade(self) -> None:
        self.assertFalse(chat_backend._rejected_json_mode(1, "rate limited"))

    def test_signals_are_never_a_downgrade(self) -> None:
        self.assertFalse(chat_backend._rejected_json_mode(
            -9, "unknown option '--mode'"))


class FakeBackendTurnTests(unittest.TestCase):
    """ChatSession.ask against a scripted phi: json path and the text fallback."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Telemetry goes to the temp dir so the test can read back what a real
        # turn records without touching the developer's logs.
        self.telemetry_dir = Path(self.tmp.name) / "telemetry"
        os.environ["PHICAD_TELEMETRY_DIR"] = str(self.telemetry_dir)
        self.addCleanup(os.environ.pop, "PHICAD_TELEMETRY_DIR", None)

    def _fake_phi(self, body: str) -> chat_backend.Backend:
        path = Path(self.tmp.name) / "phi"
        path.write_text("#!/usr/bin/env python3\nimport sys\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return chat_backend.Backend(argv=[str(path)], source="test")

    def _records(self) -> list[dict]:
        out: list[dict] = []
        for f in sorted(self.telemetry_dir.glob("*.jsonl")):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out

    def test_json_mode_turn_records_provider_reported_usage(self) -> None:
        backend = self._fake_phi(
            "assert '--mode' in sys.argv and 'json' in sys.argv\n"
            f"print({_event(texts=('BACKEND OK',))!r})\n")
        session = chat_backend.ChatSession(backend)
        self.assertEqual(session.ask("hello"), "BACKEND OK")
        record = [r for r in self._records() if r.get("outcome") == "ok"][-1]
        self.assertEqual(record.get("model"), "strong")
        self.assertEqual(record.get("tokens_out"), 7)
        self.assertEqual(record.get("cost_usd"), 0.003)
        # the session surfaces the same report for the GUI status line, summing
        # only what was reported (two turns -> exactly 2 x 0.003, no estimates)
        assert session.last_usage is not None
        self.assertEqual(session.last_usage.model, "strong")
        session.ask("again")
        self.assertAlmostEqual(session.cost_usd_total, 0.006)

    def test_old_cli_downgrades_once_and_replays_in_text(self) -> None:
        backend = self._fake_phi(
            "if '--mode' in sys.argv:\n"
            "    sys.stderr.write(\"error: unknown option '--mode'\\n\")\n"
            "    sys.exit(2)\n"
            "print('PLAIN TEXT REPLY')\n")
        session = chat_backend.ChatSession(backend)
        self.assertEqual(session.ask("hello"), "PLAIN TEXT REPLY")
        self.assertFalse(backend.json_mode)
        # the whole backend is downgraded: the next turn goes straight to text
        self.assertEqual(session.ask("again"), "PLAIN TEXT REPLY")
        ok = [r for r in self._records() if r.get("outcome") == "ok"]
        self.assertEqual(len(ok), 2)
        # a text-mode turn records sizes, never token counts it cannot know
        self.assertNotIn("tokens_out", ok[0])
        self.assertNotIn("cost_usd", ok[0])
        self.assertTrue(any(r.get("outcome") == "json_mode_unsupported"
                            for r in self._records()))

    def test_empty_event_stream_is_an_empty_reply_error(self) -> None:
        backend = self._fake_phi("print('{\"type\": \"turn_end\"}')\n")
        session = chat_backend.ChatSession(backend)
        with self.assertRaises(chat_backend.ChatError):
            session.ask("hello")


def _codex_stream(text: str = "OK", thread: str = "thread-1",
                  usage: str = '{"input_tokens": 14502, "cached_input_tokens": 11520, '
                               '"output_tokens": 7, "reasoning_output_tokens": 0}') -> str:
    """A codex exec --json event stream, shapes as captured live from 0.153."""
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": text}}),
        '{"type": "turn.completed", "usage": ' + usage + "}",
    ])


class ParseCodexEventsTests(unittest.TestCase):
    def test_extracts_text_usage_and_thread(self) -> None:
        text, usage, thread = chat_backend.parse_codex_events(_codex_stream("GPT6 OK"))
        self.assertEqual(text, "GPT6 OK")
        self.assertEqual(thread, "thread-1")
        assert usage is not None
        self.assertEqual(usage.tokens_in, 14502)
        self.assertEqual(usage.tokens_out, 7)
        self.assertEqual(usage.tokens_reasoning, 0)
        self.assertEqual(usage.cache_read, 11520)
        # codex reports no dollar cost; absent means unmeasured, never zero
        self.assertIsNone(usage.cost_usd)
        # and it never echoes the serving model; identity is the CALLER's claim
        self.assertEqual(usage.model, "")

    def test_missing_usage_fields_stay_none(self) -> None:
        _, usage, _ = chat_backend.parse_codex_events(_codex_stream(usage="{}"))
        assert usage is not None
        self.assertIsNone(usage.tokens_in)
        self.assertIsNone(usage.tokens_out)

    def test_non_json_noise_is_ignored(self) -> None:
        noisy = "Reading additional input from stdin...\n" + _codex_stream("REPLY")
        text, usage, thread = chat_backend.parse_codex_events(noisy)
        self.assertEqual(text, "REPLY")
        self.assertEqual(thread, "thread-1")
        assert usage is not None


class TurnArgvTests(unittest.TestCase):
    """One builder, two dialects: the exact command lines each flavor gets."""

    def test_phi_dialect_unchanged(self) -> None:
        backend = chat_backend.Backend(argv=["/bin/phi"], source="t")
        argv = chat_backend.turn_argv(backend, session_ref="sid",
                                      system_prompt="SYS", prompt="hello")
        self.assertEqual(argv[:2], ["/bin/phi", "--print"])
        self.assertIn("--session-id", argv)
        self.assertIn("SYS", argv)          # a flag argument, not prompt text
        self.assertEqual(argv[-1], "hello")
        self.assertNotIn("--model", argv)   # no model pinned -> no flag

    def test_phi_model_pin_becomes_flag(self) -> None:
        backend = chat_backend.Backend(argv=["/bin/phi"], source="t", model="strong")
        argv = chat_backend.turn_argv(backend, session_ref="s",
                                      system_prompt="SYS", prompt="p")
        self.assertIn("--model", argv)
        self.assertIn("strong", argv)

    def test_codex_first_turn_carries_system_prompt_in_prompt(self) -> None:
        backend = chat_backend.Backend(argv=["/bin/codex"], source="t",
                                       flavor="codex", model="gpt-6-astra")
        argv = chat_backend.turn_argv(backend, session_ref="unused",
                                      system_prompt="SYS", prompt="hello")
        self.assertEqual(argv[:2], ["/bin/codex", "exec"])
        self.assertIn("--json", argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("--model", argv)
        self.assertNotIn("resume", argv)
        # codex has no system-prompt flag: directives ride at the top of the prompt
        self.assertTrue(argv[-1].startswith("[SYSTEM DIRECTIVES]\nSYS"))
        self.assertTrue(argv[-1].endswith("hello"))

    def test_codex_resume_speaks_the_narrow_dialect(self) -> None:
        backend = chat_backend.Backend(argv=["/bin/codex"], source="t", flavor="codex")
        argv = chat_backend.turn_argv(backend, session_ref="unused",
                                      system_prompt="SYS", prompt="again",
                                      codex_thread="th-9")
        self.assertEqual(argv[1:3], ["exec", "resume"])
        self.assertIn("th-9", argv)
        # `exec resume` rejects --color/--sandbox; the thread keeps its policy
        self.assertNotIn("--color", argv)
        self.assertNotIn("--sandbox", argv)
        # and the directives are already in the thread -- prompt goes bare
        self.assertEqual(argv[-1], "again")

    def test_codex_schema_binding_flag(self) -> None:
        backend = chat_backend.Backend(argv=["/bin/codex"], source="t", flavor="codex")
        argv = chat_backend.turn_argv(backend, session_ref="u", system_prompt="",
                                      prompt="p", schema_file="/tmp/s.json")
        self.assertIn("--output-schema", argv)
        self.assertIn("/tmp/s.json", argv)


class FakeCodexTurnTests(unittest.TestCase):
    """ChatSession.ask against a scripted codex: thread resume and honest usage."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.telemetry_dir = Path(self.tmp.name) / "telemetry"
        os.environ["PHICAD_TELEMETRY_DIR"] = str(self.telemetry_dir)
        self.addCleanup(os.environ.pop, "PHICAD_TELEMETRY_DIR", None)

    def _fake_codex(self, body: str) -> chat_backend.Backend:
        path = Path(self.tmp.name) / "codex"
        path.write_text("#!/usr/bin/env python3\nimport sys\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return chat_backend.Backend(argv=[str(path)], source="test",
                                    flavor="codex", model="gpt-6-astra")

    def _records(self) -> list[dict]:
        out: list[dict] = []
        for f in sorted(self.telemetry_dir.glob("*.jsonl")):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out

    def test_turn_records_requested_model_and_verbatim_tokens(self) -> None:
        backend = self._fake_codex(
            "assert sys.argv[1] == 'exec'\n"
            f"print({_codex_stream('CODEX OK')!r})\n")
        session = chat_backend.ChatSession(backend)
        self.assertEqual(session.ask("hello"), "CODEX OK")
        self.assertEqual(session.codex_thread, "thread-1")
        record = [r for r in self._records() if r.get("outcome") == "ok"][-1]
        self.assertEqual(record.get("model"), "gpt-6-astra")   # requested == served
        self.assertEqual(record.get("provider"), "codex")
        self.assertEqual(record.get("tokens_in"), 14502)
        self.assertEqual(record.get("cache_read_tokens"), 11520)
        self.assertNotIn("cost_usd", record)  # codex reports none; never fabricated
        # the GUI cost floor stays exactly zero -- nothing was reported
        self.assertEqual(session.cost_usd_total, 0.0)

    def test_second_turn_resumes_the_minted_thread(self) -> None:
        backend = self._fake_codex(
            "if 'resume' in sys.argv:\n"
            "    assert 'thread-1' in sys.argv\n"
            f"    print({_codex_stream('RESUMED')!r})\n"
            "else:\n"
            f"    print({_codex_stream('FIRST')!r})\n")
        session = chat_backend.ChatSession(backend)
        self.assertEqual(session.ask("one"), "FIRST")
        self.assertEqual(session.ask("two"), "RESUMED")

    def test_unrequested_model_stays_unnamed(self) -> None:
        backend = self._fake_codex(f"print({_codex_stream('OK')!r})\n")
        backend.model = ""  # CLI default model: identity unknown, never guessed
        session = chat_backend.ChatSession(backend)
        session.ask("hello")
        record = [r for r in self._records() if r.get("outcome") == "ok"][-1]
        self.assertNotIn("model", record)


class ResolveBackendSelectionTests(unittest.TestCase):
    """Flavor selection: explicit choice is honored, never silently substituted."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ("PHICAD_AI_CMD", "PHICAD_AI_BACKEND", "PHICAD_AI_MODEL"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self._old_path = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)

    def _install(self, name: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return str(path)

    def test_explicit_codex_choice_is_no_longer_honoured(self) -> None:
        """$PHICAD_AI_BACKEND=codex used to win. It is now inert.

        Rewritten rather than deleted: the old assertion documented a real capability, and the
        release contract is that the capability is GONE (phi is the only harness, baked in).
        Keeping the case with an inverted assertion is what stops the feature reappearing by
        accident. Full rationale and the rest of the negatives: test_ai_backend_locked.py.
        """
        self._install("codex")
        self._install("phi")
        os.environ["PATH"] = self.tmp.name
        os.environ["PHICAD_AI_BACKEND"] = "codex"
        os.environ["PHICAD_AI_MODEL"] = "gpt-6-astra"
        backend = chat_backend.resolve_backend()
        assert backend is not None
        self.assertEqual(backend.flavor, "phi", "codex is selectable again")
        self.assertNotEqual(backend.model, "gpt-6-astra", "the model pin is honoured again")

    def test_phi_wins_by_default(self) -> None:
        self._install("codex")
        self._install("phi")
        os.environ["PATH"] = self.tmp.name
        backend = chat_backend.resolve_backend()
        assert backend is not None
        self.assertEqual(backend.flavor, "phi")

    def test_codex_cmd_override_is_no_longer_honoured(self) -> None:
        """$PHICAD_AI_CMD pointed at codex used to select it, flavor sniffed from argv[0].

        Now ignored: an environment variable that chooses which binary receives the board
        context and the user's prompts is a supply-chain hole, not a convenience.
        """
        exe = self._install("codex")
        self._install("phi")
        os.environ["PATH"] = self.tmp.name
        os.environ["PHICAD_AI_CMD"] = exe
        backend = chat_backend.resolve_backend()
        assert backend is not None
        self.assertEqual(backend.flavor, "phi", "$PHICAD_AI_CMD still selects a binary")
        self.assertNotEqual(backend.source, "$PHICAD_AI_CMD")


if __name__ == "__main__":
    unittest.main(verbosity=2)
