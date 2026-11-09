#!/usr/bin/env python3
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
"""Self-test for telemetry.  Stdlib only, no build tree, no network.

The properties worth asserting here are the ones whose failure is silent:

  1.  never raises    a full disk must not be able to end a design session
  2.  opt-out         PHICAD_TELEMETRY=0 writes nothing at all
  3.  no nulls        an unmeasured field is absent, not null -- null averages as zero
  4.  span            times the block, names the exception class, re-raises untouched
  5.  no content      the AI call site records SIZES; no prompt or reply text reaches
                      the file.  This is the one that matters: it is the difference
                      between a metrics file and a copy of everyone's circuit designs.
  6.  reader          nearest-rank quantiles, corrupt lines skipped, unknown schema flagged

Exit 0 = pass.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "plugins" / "ai-view"))

import telemetry  # noqa: E402
import telemetry_report  # noqa: E402


def records(directory: Path) -> list[dict]:
    out = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def check_never_raises() -> None:
    # A directory that cannot be created, a sink that throws, and a value json
    # cannot serialise -- none of them may reach the caller.
    os.environ["PHICAD_TELEMETRY_DIR"] = "/dev/null/cannot-exist"
    telemetry.emit("ai.turn", outcome="ok")
    with telemetry.span("synth.run", command="build"):
        pass

    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        os.environ["PHICAD_TELEMETRY_DIR"] = tmp
        telemetry.set_sink(lambda rec: (_ for _ in ()).throw(RuntimeError("sink down")))
        telemetry.emit("ai.turn", outcome="ok")
        telemetry.set_sink(None)
        # default=str in the encoder keeps an exotic value from raising
        telemetry.emit("ai.turn", outcome="ok", odd=object())
        assert len(records(Path(tmp))) == 2, "a raising sink must not lose the record"
    print("ok 1 - never raises: unwritable dir, throwing sink, unserialisable value")


def check_opt_out() -> None:
    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        for value in ("0", "false", "NO", "off"):
            os.environ["PHICAD_TELEMETRY_DIR"] = tmp
            os.environ["PHICAD_TELEMETRY"] = value
            assert not telemetry.enabled(), f"{value!r} should disable telemetry"
            telemetry.emit("ai.turn", outcome="ok")
        assert not list(Path(tmp).glob("*.jsonl")), "opt-out must write no file at all"
        os.environ.pop("PHICAD_TELEMETRY", None)
        telemetry.emit("ai.turn", outcome="ok")
        assert len(records(Path(tmp))) == 1, "removing the opt-out must re-enable"
    print("ok 2 - opt-out honours 0/false/no/off and writes nothing")


def check_no_nulls() -> None:
    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        os.environ["PHICAD_TELEMETRY_DIR"] = tmp
        telemetry.emit("ai.turn", outcome="ok", measured=3, not_measured=None)
        rec = records(Path(tmp))[0]
        assert "not_measured" not in rec, "an unmeasured field must be absent, not null"
        assert rec["measured"] == 3
        assert rec["schema"] == telemetry.SCHEMA and rec["event"] == "ai.turn"
        assert rec["ts"].endswith("Z"), "timestamps are UTC"
    print("ok 3 - unmeasured fields are omitted, not written as null")


def check_span() -> None:
    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        os.environ["PHICAD_TELEMETRY_DIR"] = tmp
        with telemetry.span("synth.run", command="build") as add:
            add(stages_total=14)
        good = records(Path(tmp))[-1]
        assert good["outcome"] == "ok" and good["stages_total"] == 14
        assert isinstance(good["duration_ms"], (int, float))

        raised = False
        try:
            with telemetry.span("synth.run", command="check"):
                raise KeyError("nope")
        except KeyError:
            raised = True
        assert raised, "span must re-raise, not swallow"
        bad = records(Path(tmp))[-1]
        assert bad["outcome"] == "error" and bad["error_type"] == "KeyError"
        assert bad["command"] == "check", "fields given up front survive the failure"
    print("ok 4 - span times the block, names the exception, and re-raises")


def check_no_content_leaks() -> None:
    """The AI call site must record sizes and never the text."""
    import chat_backend as cb

    secret_msg = "SECRET-PROMPT-a1b2c3"
    secret_ctx = "SECRET-CONTEXT-d4e5f6"
    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        os.environ["PHICAD_TELEMETRY_DIR"] = tmp
        # /usr/bin/false exits nonzero: exercises a real recorded path with no
        # dependency on an AI backend being installed.
        session = cb.ChatSession(backend=cb.Backend(argv=["/usr/bin/false"], source="selftest"))
        try:
            session.ask(secret_msg, context=secret_ctx)
        except cb.ChatError:
            pass

        blob = "\n".join(p.read_text(encoding="utf-8")
                         for p in Path(tmp).glob("*.jsonl"))
        assert secret_msg not in blob, "the prompt must never be recorded"
        assert secret_ctx not in blob, "the board context must never be recorded"
        assert cb.SYSTEM_PROMPT[:40] not in blob, "the system prompt must never be recorded"

        rec = records(Path(tmp))[0]
        assert rec["event"] == "ai.turn" and rec["outcome"] == "exit_nonzero"
        assert rec["exit_code"] == 1
        assert rec["has_context"] is True
        assert rec["context_bytes"] == len(secret_ctx.encode("utf-8"))
        assert rec["prompt_bytes"] == len(
            cb.ChatSession.compose(secret_msg, secret_ctx).encode("utf-8"))
        assert "reply_bytes" not in rec, "a failed turn has no reply to size"
        # No field may carry free text.  Names are a fixed vocabulary; if one is
        # added that holds content, this is where it gets caught.
        allowed = {"ts", "schema", "event", "session_id", "turn", "backend",
                   "backend_version", "backend_source", "has_context", "context_bytes",
                   "prompt_bytes", "reply_bytes", "timeout_s", "duration_ms",
                   "outcome", "exit_code", "signal", "error_type"}
        unexpected = set(rec) - allowed
        assert not unexpected, f"unexpected field(s) on ai.turn: {sorted(unexpected)}"
    print("ok 5 - the AI call site records sizes; no prompt, context or reply text")


def check_reader() -> None:
    q = telemetry_report.quantile
    assert q([], 0.5) == 0.0
    assert q([5], 0.9) == 5
    assert q([1, 2, 3], 0.5) == 2
    assert q([1, 2, 3, 4], 0.9) == 4, "nearest-rank, no interpolation"
    # The property the report is sold on: one outlier moves p90, not the median.
    many = [2] * 19 + [300]
    assert q(many, 0.5) == 2 and q(many, 0.9) == 2 and q(many, 0.99) == 300

    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        os.environ["PHICAD_TELEMETRY_DIR"] = tmp
        telemetry.emit("ai.turn", session_id="s1", outcome="ok", duration_ms=10,
                       backend="phi", prompt_bytes=100, reply_bytes=50, has_context=True)
        telemetry.emit("ai.turn", session_id="s1", outcome="timeout", duration_ms=9000,
                       backend="phi", prompt_bytes=120, has_context=True)
        telemetry.emit("synth.run", command="build", outcome="failed", duration_ms=500,
                       stages_total=14, stages_passed=13, failed_stages=["route"])
        # a half-written record, as a killed editor leaves behind
        day = telemetry.current_log()
        with day.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "ai.turn", "outcome":\n')
        # and one from a future schema
        telemetry.emit("ai.turn", outcome="ok", duration_ms=1)
        text = day.read_text(encoding="utf-8").replace('"schema": 1', '"schema": 99', 1)
        day.write_text(text, encoding="utf-8")

        summary = telemetry_report.summarise(list(telemetry_report.read(1)))
        assert summary["records"] == 4, "the corrupt line is skipped, not fatal"
        assert summary["ai_turns"]["total"] == 3
        assert summary["ai_turns"]["sessions"] == 1
        assert summary["ai_turns"]["outcomes"]["timeout"] == 1
        assert summary["ai_turns"]["with_board_context"] == 2
        assert summary["synth_runs"]["failing_stages"] == {"route": 1}
        assert summary["unknown_schema_versions"] == [99], "a newer schema is flagged"
        assert "schema 99" in telemetry_report.human(summary, 1) or \
               "[99]" in telemetry_report.human(summary, 1)
        # reply stats come from successful turns only
        assert summary["ai_turns"]["reply_bytes"]["median"] == 50
    print("ok 6 - reader: nearest-rank quantiles, corrupt lines skipped, schema flagged")


def check_installed_layout() -> None:
    """An installed plugin is a real directory outside the repo; pylib is not beside it.

    `phicad ai-view-install` copies plugins/ai-view to ~/Documents/KiCad/<ver>/plugins/,
    because wxDir::Traverse skips symlinked directories.  A plain
    `parents[2] / "pylib"` therefore resolves to a path that does not exist, and an
    import guarded by try/except then quietly does nothing in the ONLY configuration
    real users run -- a telemetry system that records only from a checkout.

    So this runs the resolver from a copy laid out exactly as the installer leaves it.
    """
    import subprocess

    with tempfile.TemporaryDirectory(prefix="phicad-install-") as tmp:
        plugin_src = HERE.parent / "plugins" / "ai-view"
        dest = Path(tmp) / "KiCad" / "10.99" / "plugins" / "ai-view"
        dest.parent.mkdir(parents=True)
        subprocess.run(["cp", "-R", str(plugin_src), str(dest)], check=True)
        (dest / "repo-path").write_text(str(HERE.parents[1]) + "\n", encoding="utf-8")

        assert not (dest.parents[2] / "pylib").exists(), \
            "fixture is wrong: pylib must NOT be beside an installed plugin"

        logs = Path(tmp) / "telemetry"
        probe = (
            "import sys; sys.path.insert(0, %r)\n"
            "import chat_backend as cb\n"
            "assert cb.telemetry is not None, 'telemetry unreachable from an installed copy'\n"
            "s = cb.ChatSession(backend=cb.Backend(argv=['/usr/bin/false'], source='t'))\n"
            "try: s.ask('probe')\n"
            "except cb.ChatError: pass\n"
            "print('ok')\n" % str(dest)
        )
        env = dict(os.environ, PHICAD_TELEMETRY_DIR=str(logs))
        env.pop("PHICAD_TELEMETRY", None)
        env.pop("PHICAD_REPO", None)          # must work off repo-path alone
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                              text=True, env=env)
        assert proc.returncode == 0, f"installed-layout probe failed:\n{proc.stderr}"
        written = records(logs)
        assert len(written) == 1 and written[0]["outcome"] == "exit_nonzero", \
            "an installed plugin must still record"
    print("ok 7 - telemetry is reachable from an installed plugin copy, via repo-path")


def check_repair_loop_is_visible() -> None:
    """The automatic repair loop uses its OWN ask(), and it must be recorded too.

    design_agent.PhiCliDesignModel.ask is a second implementation of the same phi call,
    and DesignValidationWorkflow drives it up to PHICAD_AI_REPAIR_ATTEMPTS times per
    validation -- so most of the product's model calls come through it rather than
    through the chat pane.  Instrumenting only the pane would leave `phicad telemetry`
    reporting a minority of real turns while looking authoritative.
    """
    import chat_backend as cb
    import design_agent as da

    secret = "SECRET-REPAIR-PROMPT-9z8y7x"
    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        os.environ["PHICAD_TELEMETRY_DIR"] = tmp
        model = da.PhiCliDesignModel(
            backend=cb.Backend(argv=["/usr/bin/false"], source="selftest"),
            system_prompt="unused")
        try:
            model.ask(secret)
        except cb.ChatError:
            pass

        rec = records(Path(tmp))[0]
        assert rec["event"] == "ai.turn", "the repair loop's turns are ai.turn too"
        assert rec["source"] == "design_agent", "and must be tellable apart from the pane"
        assert rec["outcome"] == "exit_nonzero" and rec["exit_code"] == 1
        assert rec["prompt_bytes"] == len(secret.encode("utf-8"))
        blob = "\n".join(p.read_text(encoding="utf-8") for p in Path(tmp).glob("*.jsonl"))
        assert secret not in blob, "the repair prompt must never be recorded either"
    print("ok 8 - the automatic repair loop's model calls are recorded, and tagged")


def check_cycles_are_folded() -> None:
    """N attempts of one repair cycle must not read as N-1 failures."""
    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        os.environ["PHICAD_TELEMETRY_DIR"] = tmp
        # one cycle: fails twice, then succeeds -- a design that repaired itself
        for attempt, outcome in ((0, "failed"), (1, "failed"), (2, "ok")):
            telemetry.emit("synth.run", command="check", outcome=outcome,
                           duration_ms=10, run_id="cycle-1", attempt=attempt)
        # and one ordinary run that nothing was driving
        telemetry.emit("synth.run", command="build", outcome="ok", duration_ms=20)

        syn = telemetry_report.summarise(list(telemetry_report.read(1)))["synth_runs"]
        assert syn["total"] == 4, "every invocation is still counted"
        assert syn["attempt_outcomes"] == {"failed": 2, "ok": 2}, "attempts stay truthful"
        assert syn["repair_cycles"] == 1 and syn["standalone"] == 1
        assert syn["cycle_outcomes"] == {"ok": 1}, "a cycle is judged on its last attempt"
        assert syn["self_repaired"] == 1
        text = telemetry_report.human({"records": 4, "unknown_schema_versions": [],
                                       "ai_turns": {"total": 0, "sessions": 0,
                                                    "with_board_context": 0},
                                       "synth_runs": syn}, 1)
        assert "repair cycle" in text and "repaired themselves" in text
    print("ok 9 - a repair cycle is one cycle, judged on its last attempt")


def check_line_protocol() -> None:
    """Encoding, and the cardinality rule that makes or breaks an Influx bucket."""
    import telemetry_influx as influx

    rec = {"ts": "2026-09-06T22:41:17Z", "schema": 1, "event": "ai.turn",
           "source": "chat_pane", "outcome": "ok", "backend": "phi",
           "session_id": "s-1", "duration_ms": 1200, "has_context": True,
           "timeout_s": 300.0, "failed_stages": ["erc", "route"]}
    got = influx.line(rec)
    head, fields, stamp = got.split(" ")

    # THE cardinality rule.  A UUID in a tag creates one index series per session,
    # forever; the index outgrows the data and the only fix is a rewrite.
    tags = head.split(",")[1:]
    tag_keys = {t.split("=")[0] for t in tags}
    assert tag_keys <= set(influx.TAG_KEYS), f"unexpected tag(s): {tag_keys}"
    for forbidden in ("session_id", "run_id", "attempt", "duration_ms"):
        assert forbidden not in tag_keys, f"{forbidden} must be a field, never a tag"
    assert 'session_id="s-1"' in fields, "session_id must still be recorded, as a field"

    assert "duration_ms=1200i" in fields, "ints carry the i suffix"
    assert "has_context=true" in fields, "bool before int, or a flag ships as 1i"
    assert "timeout_s=300.0" in fields, "floats stay floats"
    assert 'failed_stages="erc,route"' in fields, "a list joins rather than fanning out"
    # the record's own time, not now()
    assert stamp == str(influx.to_nanos("2026-09-06T22:41:17Z"))
    assert stamp.endswith("000000000") and len(stamp) == 19

    # Separators inside values must be escaped or the line silently reparses wrong.
    nasty = influx.line({"ts": "2026-09-06T00:00:00Z", "event": "ai.turn",
                         "backend": "we ird,name=x", "note": 'say "hi"\\now',
                         "duration_ms": 1})
    assert r"backend=we\ ird\,name\=x" in nasty, "tag separators must be escaped"
    assert r'note="say \"hi\"\\now"' in nasty, "quotes and backslashes must be escaped"

    # A point with tags but no fields is rejected by Influx; drop it here instead.
    assert influx.line({"ts": "2026-09-06T00:00:00Z", "event": "ai.turn"}) is None
    print("ok 10 - line protocol: escaping, types, and no unbounded tag")


def check_ship_cursor() -> None:
    """Shipping is resumable, ships each line once, and never ships a partial line."""
    import telemetry_influx as influx

    with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
        log_dir = Path(tmp)
        day = log_dir / "2026-09-06.jsonl"
        day.write_text('{"ts":"2026-09-06T00:00:01Z","event":"ai.turn","duration_ms":1}\n'
                       '{"ts":"2026-09-06T00:00:02Z","event":"ai.turn","duration_ms":2}\n',
                       encoding="utf-8")

        offsets: dict = {}
        batches = list(influx.pending(log_dir, offsets))
        assert len(batches) == 1 and len(batches[0][2]) == 2
        offsets[day.name] = batches[0][1]
        assert not list(influx.pending(log_dir, offsets)), "nothing re-ships"

        # append one whole line and one that is still being written
        with day.open("a", encoding="utf-8") as handle:
            handle.write('{"ts":"2026-09-06T00:00:03Z","event":"ai.turn","duration_ms":3}\n')
            handle.write('{"ts":"2026-09-06T00:00:04Z","event":"ai.tu')
        batches = list(influx.pending(log_dir, offsets))
        assert len(batches) == 1, "the appended line is picked up"
        assert len(batches[0][2]) == 1, "and ONLY the complete one"
        assert '"duration_ms":3' in batches[0][2][0]
        offsets[day.name] = batches[0][1]

        # finish the torn line: now it ships, exactly once
        with day.open("a", encoding="utf-8") as handle:
            handle.write('rn","duration_ms":4}\n')
        batches = list(influx.pending(log_dir, offsets))
        assert len(batches) == 1 and len(batches[0][2]) == 1
        assert '"duration_ms":4' in batches[0][2][0]

        influx.write_cursor(log_dir, {day.name: batches[0][1]})
        assert influx.read_cursor(log_dir) == {day.name: batches[0][1]}
        # a corrupt cursor must not re-ship silently under a wrong offset
        influx.cursor_path(log_dir).write_text("{not json", encoding="utf-8")
        assert influx.read_cursor(log_dir) == {}, "an unreadable cursor starts over"
    print("ok 11 - ship cursor: resumable, exactly-once, never a partial line")


def check_influx_config() -> None:
    """No default endpoint, and a token file that anyone can read is refused."""
    import telemetry_influx as influx

    saved = {k: os.environ.get(k) for k in
             ("PHICAD_INFLUX_URL", "PHICAD_INFLUX_ORG", "PHICAD_INFLUX_BUCKET",
              "PHICAD_INFLUX_TOKEN", "PHICAD_INFLUX_TOKEN_FILE")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        try:
            influx.load_config()
            raise AssertionError("an unconfigured shipper must refuse, not guess a host")
        except influx.ShipError as exc:
            assert "PHICAD_INFLUX_URL" in str(exc)

        with tempfile.TemporaryDirectory(prefix="phicad-tel-") as tmp:
            token = Path(tmp) / "token"
            token.write_text("secret-token", encoding="utf-8")
            token.chmod(0o644)
            os.environ.update({"PHICAD_INFLUX_URL": "https://influx.example",
                               "PHICAD_INFLUX_ORG": "gateway",
                               "PHICAD_INFLUX_BUCKET": "phicad",
                               "PHICAD_INFLUX_TOKEN_FILE": str(token)})
            try:
                influx.load_config()
                raise AssertionError("a world-readable token file must be refused")
            except influx.ShipError as exc:
                assert "readable" in str(exc)

            token.chmod(0o600)
            config = influx.load_config()
            assert config.token == "secret-token"
            assert "precision=ns" in config.write_url and "bucket=phicad" in config.write_url
            assert "secret-token" not in config.redacted(), "redacted() must not leak the token"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("ok 12 - influx config: refuses to guess a host, refuses a loose token file")


def main() -> int:
    saved = {k: os.environ.get(k) for k in ("PHICAD_TELEMETRY", "PHICAD_TELEMETRY_DIR")}
    try:
        check_never_raises()
        check_opt_out()
        check_no_nulls()
        check_span()
        check_no_content_leaks()
        check_reader()
        check_installed_layout()
        check_repair_loop_is_visible()
        check_cycles_are_folded()
        check_line_protocol()
        check_ship_cursor()
        check_influx_config()
    finally:
        telemetry.set_sink(None)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("telemetry selftest: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
