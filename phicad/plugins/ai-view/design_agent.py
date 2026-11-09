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
"""Model-neutral design turns and the automatic synth repair loop.

This module deliberately knows nothing about Tk.  A PhiCad window, a host bridge, or a
future headless service can drive the same workflow and receive the same versioned events.
The current adapter invokes ``phi``; a future model adapter (including a GPT-6 adapter when
its real API and capabilities exist) implements ``DesignModel`` without changing validation,
approval, synthesis, or host integration code.

The safety boundary is intentional: the model may revise an in-memory design until the
machine checker accepts it, but it never builds a board.  A host still asks the human to
approve the final validated design before starting synthesis.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

import chat_backend
from design_commands import DesignError, DesignSession, extract_design

telemetry = chat_backend.telemetry

EVENT_SCHEMA = "phicad.design-workflow.event/v1"
DEFAULT_MAX_REPAIRS = 8


@dataclass(frozen=True)
class ModelCapabilities:
    """Capabilities a host may negotiate instead of branching on a model name."""

    protocol_version: int = 1
    provider: str = "unknown"
    model: str = "unknown"
    structured_output: bool = False
    tool_calls: bool = False
    streaming: bool = False
    vision: bool = False
    background_tasks: bool = False
    session_continuity: bool = False
    extras: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "provider": self.provider,
            "model": self.model,
            "structured_output": self.structured_output,
            "tool_calls": self.tool_calls,
            "streaming": self.streaming,
            "vision": self.vision,
            "background_tasks": self.background_tasks,
            "session_continuity": self.session_continuity,
            "extras": dict(self.extras),
        }


class DesignModel(Protocol):
    """Small provider boundary required by the design workflow."""

    @property
    def capabilities(self) -> ModelCapabilities:
        ...

    def ask(self, message: str) -> str:
        """Return one model turn or raise ``chat_backend.ChatError``."""
        ...


@dataclass
class PhiCliDesignModel:
    """Current model adapter: one persistent, design-only ``phi`` conversation."""

    backend: chat_backend.Backend
    system_prompt: str
    workdir: Optional[str] = None
    timeout_s: float = field(default_factory=lambda: float(
        os.environ.get("PHICAD_AI_TIMEOUT", "600")))
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Identity of the model that actually answered the last turn, from the
    # provider's own usage report.  Before the first turn it is unknown — the
    # capability handshake says so rather than guessing from configuration,
    # because the user can switch models in phi at any moment.
    last_usage: Optional[chat_backend.TurnUsage] = None
    # codex session handle, minted on the first turn (see chat_backend).
    codex_thread: str = ""
    # JSON schema file to bind the model's final message to, for backends whose
    # dialect supports it (codex --output-schema).  Set by the caller to the
    # design-IR schema; silently unused where the capability does not exist —
    # the repair loop remains the universal fallback.
    schema_file: Optional[str] = None

    @property
    def capabilities(self) -> ModelCapabilities:
        live = self.last_usage
        codex = self.backend.flavor == "codex"
        if live and live.model:
            model = live.model
        else:
            model = (self.backend.model or self.backend.version
                     or "configured-provider")
        if codex:
            # codex never echoes the serving model; "requested" is the honest
            # strongest claim (the CLI fails the turn on a model it cannot serve).
            identity = "requested" if self.backend.model else "cli-default"
        else:
            identity = "reported-per-turn" if live else "unknown-until-first-turn"
        return ModelCapabilities(
            provider=(live.provider if live and live.provider
                      else ("codex" if codex else "phi-cli")),
            model=model,
            session_continuity=True,
            structured_output=codex,   # the DIALECT can bind schemas (--output-schema)
            extras={
                "transport": "subprocess",
                "backend_source": self.backend.source,
                "backend_flavor": self.backend.flavor,
                "design_schema": "https://couplet-physical-ai.github.io/PhiCad/schemas/design-ir.v1.json",
                # Whether THIS session's replies are provider-enforced against the
                # design schema.  False for design-ir under codex today: its open-
                # key maps exceed the strict subset (see design_ir_schema_file),
                # so formatting is guaranteed by the repair loop instead.
                "design_schema_bound": bool(self.schema_file),
                # True once the backend has proven it reports real usage; a host
                # can gate cost display on this instead of probing again.
                "usage_reporting": (True if codex else bool(self.backend.json_mode)),
                "model_identity": identity,
            },
        )

    def ask(self, message: str) -> str:
        argv = chat_backend.turn_argv(
            self.backend, session_ref=self.session_id,
            system_prompt=self.system_prompt, prompt=message,
            codex_thread=self.codex_thread, schema_file=self.schema_file)

        # This is a second, parallel implementation of ChatSession.ask, and the repair
        # loop drives it up to PHICAD_AI_REPAIR_ATTEMPTS times per validation -- so most
        # of the model calls the product makes come through HERE, not through the chat
        # pane.  Instrumenting only the pane would leave `phicad telemetry` reporting a
        # minority of real turns while looking authoritative, which is worse than
        # reporting nothing.  `source` keeps the two tellable apart.
        #
        # Sizes only, never content: see phicad/pylib/telemetry.py.
        started = time.monotonic()

        def record(outcome: str, **extra) -> None:
            if telemetry is None:
                return
            telemetry.emit(
                "ai.turn",
                source="design_agent",
                session_id=self.session_id,
                backend=os.path.basename(self.backend.argv[0]),
                backend_version=self.backend.version or None,
                backend_source=self.backend.source,
                has_context=False,
                prompt_bytes=len(message.encode("utf-8")),
                timeout_s=self.timeout_s,
                duration_ms=round((time.monotonic() - started) * 1000),
                outcome=outcome,
                **extra)

        env = chat_backend.child_env()
        # This is a design-only turn.  Letting a manager-injected socket leak into phi
        # starts the KiCad MCP server against the window that launched us and can deadlock
        # a turn.  Board tools belong to the later build/open phases, not model drafting.
        env.pop("KICAD_API_SOCKET", None)
        env.pop("KICAD_API_TOKEN", None)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=self.workdir,
                env=env,
                # An explicitly closed stdin, not an inherited one: codex blocks
                # reading a piped stdin ("Reading additional input...") and a
                # design turn must never wait on a terminal.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            record("timeout")
            raise chat_backend.ChatError(
                f"phi timed out after {self.timeout_s:.0f}s "
                "(set PHICAD_AI_TIMEOUT to change)"
            ) from None
        except OSError as exc:
            record("launch_failed", error_type=type(exc).__name__)
            raise chat_backend.ChatError(f"could not launch phi: {exc}") from exc

        if proc.returncode != 0 and self.backend.flavor == "codex" \
                and self.schema_file \
                and "invalid_json_schema" in ((proc.stdout or "") + (proc.stderr or "")):
            # The provider-side validator rejected the derived schema (its accepted
            # subset shifts release to release).  Binding is an enhancement, not a
            # requirement: drop it for this session, record the downgrade, replay —
            # the repair loop still guarantees valid JSON the slow way.
            self.schema_file = None
            record("schema_binding_unsupported")
            return self.ask(message)

        if proc.returncode != 0 and self.backend.flavor == "phi" \
                and self.backend.json_mode \
                and chat_backend._rejected_json_mode(proc.returncode, proc.stderr or ""):
            # Old CLI: downgrade once, replay this turn in text mode.  The session id
            # makes the replay identical; usage is simply not recorded from then on.
            self.backend.json_mode = False
            record("json_mode_unsupported")
            return self.ask(message)

        usage = None
        if proc.returncode == 0 and self.backend.flavor == "codex":
            reply, usage, thread = chat_backend.parse_codex_events(proc.stdout or "")
            if thread:
                self.codex_thread = thread
            if usage:
                # Requested model recorded, never echoed — see chat_backend.
                usage = replace(usage, model=self.backend.model,
                                provider="codex")
        elif proc.returncode == 0 and self.backend.json_mode:
            reply, usage = chat_backend.parse_print_events(proc.stdout or "")
        else:
            reply = (proc.stdout or "").strip()
        if proc.returncode != 0 or not reply:
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
            # This adapter collapses two distinct failures into one error; the record
            # keeps them apart, because "the model returned nothing" and "the process
            # died" have different causes and different fixes.
            record("empty_reply" if proc.returncode == 0 else "exit_nonzero",
                   exit_code=proc.returncode)
            raise chat_backend.ChatError(
                f"phi exited with code {proc.returncode}"
                + (" and an empty reply" if not reply else "")
                + (f"\nstderr tail:\n{tail}" if tail else "")
            )
        if usage is not None:
            self.last_usage = usage
        record("ok", reply_bytes=len(reply.encode("utf-8")),
               **(usage.telemetry_fields() if usage else {}))
        return reply


def design_ir_schema_file(backend: chat_backend.Backend) -> Optional[str]:
    """Path to a design-IR JSON schema the backend can bind output to.

    None unless the backend's dialect has structured output (codex
    --output-schema) AND the schema file is actually present — an installed
    plugin outside a checkout has no phicad/synth, and passing a dangling path
    would fail every turn to gain nothing: the repair loop already handles
    free-form JSON.

    What is bound is a DERIVED copy, not the source schema: OpenAI's strict
    structured-output validator accepts a subset of JSON Schema (no `oneOf`,
    …), so the source contract is transformed just enough to pass
    (`_codex_safe_schema`).  The looser copy only constrains what the model
    EMITS; everything downstream still validates against the strict original,
    so nothing is accepted that the real contract would reject.

    Today this returns None for design-ir itself: the contract's load-bearing
    shape — `parts`/`nets` as open-key maps (schema-valued
    additionalProperties) — is outside the subset entirely (probed live: even
    a one-key map schema is rejected with "Extra required key").  The
    detection is structural, so the day the validator learns maps, or the day
    a fitting schema is passed, binding turns on without a code change.
    """
    if backend.flavor != "codex":
        return None
    try:
        import design_commands  # local import: design_commands pulls in synth paths

        root = design_commands.repo_root()
    except Exception:
        return None
    if root is None:
        return None
    schema = root / "phicad" / "synth" / "design-ir.schema.json"
    if not schema.is_file():
        return None
    try:
        return _codex_safe_schema(schema)
    except Exception:
        return None  # a broken derivation must not take the design flow down


def _codex_safe_schema(source: Path) -> Optional[str]:
    """Rewrite the schema into OpenAI's structured-output subset; return a path.

    None when the schema cannot fit the subset at all — an object with
    schema-valued `additionalProperties` is an open-key map, and the strict
    validator has no spelling for one (probed live, not assumed).  Returning
    None beats emitting a schema that fails every first turn at the provider.

    Transformations, matching what OpenAI's validator actually rejected (each
    one was added against a real error, never a guessed restriction):

      * `oneOf` -> `anyOf` — exclusivity dropped, choices kept (widens).
      * every object with `properties` lists ALL of them in `required`, and a
        property that was optional becomes nullable (`anyOf: [orig, null]`) —
        strict mode's way of spelling optionality.  This lets the model emit
        `null` where the source contract says "absent", so the consumer strips
        null-valued keys before validating against the original
        (design_commands.extract_design).
      * `additionalProperties: false` where an object declares properties and
        left it unset (the open `part` attribute map keeps its schema-valued
        additionalProperties — it declares no properties).

    The derived file is content-addressed in the temp dir, so repeated runs
    reuse it and a schema edit produces a fresh one.
    """
    import hashlib
    import tempfile

    data = json.loads(source.read_text(encoding="utf-8"))
    unfit: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("additionalProperties"), dict):
                unfit.append("open-key map (schema-valued additionalProperties)")
            if "oneOf" in node and "anyOf" not in node:
                node["anyOf"] = node.pop("oneOf")
            # Constraint keywords the validator rejects outright.  Removing a
            # constraint only ever WIDENS the derived contract, so each drop is
            # sound; each entry was added against a real validator error.
            node.pop("uniqueItems", None)
            node.pop("propertyNames", None)
            props = node.get("properties")
            if isinstance(props, dict) and props:
                originally_required = set(node.get("required") or [])
                for key in list(props):
                    if key not in originally_required:
                        props[key] = {"anyOf": [props[key], {"type": "null"}]}
                node["required"] = sorted(props.keys())
                node.setdefault("additionalProperties", False)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if unfit:
        return None
    blob = json.dumps(data, indent=2, sort_keys=True)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"phicad-design-ir-{digest}.schema.json"
    if not path.is_file():
        path.write_text(blob, encoding="utf-8")
    return str(path)


@dataclass(frozen=True)
class WorkflowEvent:
    """Transport-neutral progress record consumed by Tk today and other hosts later."""

    kind: str
    message: str
    attempt: int = 0
    max_attempts: int = 0
    data: Mapping[str, Any] = field(default_factory=dict)
    session_id: str = ""
    sequence: int = 0
    schema: str = EVENT_SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "message": self.message,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class ValidationOutcome:
    """Final state of a check/repair cycle; expected failures are data, not exceptions."""

    ok: bool
    design: dict
    report: Mapping[str, Any]
    repairs: int
    last_reply: str = ""
    failure: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "design": copy.deepcopy(self.design),
            "design_digest": design_digest(self.design),
            "report": dict(self.report),
            "repairs": self.repairs,
            "failure": self.failure,
        }


EventSink = Callable[[WorkflowEvent], None]


def design_digest(design: Mapping[str, Any]) -> str:
    """Stable identity used to bind human approval to the design that was reviewed."""
    payload = json.dumps(design, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _max_repairs_from_env() -> int:
    raw = os.environ.get("PHICAD_AI_REPAIR_ATTEMPTS", str(DEFAULT_MAX_REPAIRS))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_REPAIRS


# One history line per attempt, and the loop allows up to PHICAD_AI_REPAIR_ATTEMPTS of them,
# so an unbounded line is a prompt-size bug: a single 5 kB DRC message repeated across 8
# attempts is 40 kB of context spent restating one failure.  The full current report is in
# the prompt anyway -- history only has to be recognisable.
SUMMARY_LIMIT = 240


def _clip(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = " ".join(text.split())          # collapse newlines; one history line per attempt
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _rejection_summary(report: Mapping[str, Any]) -> str:
    """One line naming why a gate rejected an attempt, for the repair history.

    Keeps the prompt addition small and specific: the failing stage or the first couple of
    problems, not a second copy of the whole report (the current report is already in the
    prompt in full).
    """
    failed = report.get("failed")
    if isinstance(failed, list) and failed:
        stages = ", ".join(str(name) for name in failed[:3])
        if len(failed) > 3:
            stages += f" (+{_count(len(failed) - 3, 'more stage')})"
        return _clip(f"build gate(s) failed: {stages}")

    problems = report.get("problems")
    if isinstance(problems, list) and problems:
        shown = []
        for problem in problems[:2]:
            if isinstance(problem, Mapping):
                where = problem.get("where", "?")
                shown.append(f"{where}: {problem.get('message', '')}".strip())
        remaining = len(problems) - len(shown)
        text = "; ".join(part for part in shown if part)
        if remaining > 0:
            text += f" (+{_count(remaining, 'more problem')})"
        return _clip(text) or "rejected with no problem detail"

    return "rejected with no problem detail"


def _error_report(source: str, message: str, hint: str = "") -> dict[str, Any]:
    problem = {
        "severity": "error",
        "where": source,
        "message": message,
    }
    if hint:
        problem["hint"] = hint
    return {"ok": False, "source": source, "problems": [problem]}


def _count(number: int, singular: str, plural: str = "") -> str:
    """"1 problem" / "2 problems" — these strings are read by people, not parsers."""
    word = singular if number == 1 else (plural or f"{singular}s")
    return f"{number} {word}"


def _problem_count(report: Mapping[str, Any]) -> int:
    problems = report.get("problems", [])
    if not isinstance(problems, list):
        return 1
    hard = [problem for problem in problems
            if not isinstance(problem, dict) or problem.get("severity", "error") == "error"]
    return len(hard) or len(problems)


class DesignValidationWorkflow:
    """Check a design and feed every failure back to the model until it validates."""

    def __init__(self, session: DesignSession, model: Optional[DesignModel],
                 max_repairs: Optional[int] = None) -> None:
        self.session = session
        self.model = model
        self.max_repairs = (_max_repairs_from_env()
                            if max_repairs is None else max(0, max_repairs))
        self.session_id = str(uuid.uuid4())
        self._sequence = 0

    @property
    def capabilities(self) -> dict[str, Any]:
        return (self.model.capabilities.as_dict() if self.model is not None
                else ModelCapabilities().as_dict())

    @staticmethod
    def repair_prompt(design: Mapping[str, Any], feedback: Mapping[str, Any],
                      attempt: int, max_attempts: int,
                      history: Optional[Sequence[Mapping[str, Any]]] = None) -> str:
        """Prompt contract shared by preflight failures and failed build gates.

        `history` is what the loop already tried and what the gate said about it.  Without
        it every round looks like the first one to the model: on a real routing failure it
        widened tracks when the hint said narrow, then proposed a variation of the same
        change, because nothing in the prompt showed that approach had already been
        rejected.  Naming the rejected approaches is what makes the bound meaningful --
        otherwise the loop can spend all its attempts circling one wrong idea.
        """
        parts = [
            "[AUTOMATIC PHICAD VALIDATION FEEDBACK]\n"
            "A machine gate rejected the current design. This feedback is being sent "
            "to you automatically; do not ask the user to copy, paste, or diagnose it.\n\n"
            f"Repair attempt: {attempt} of {max_attempts}\n"
        ]

        if history:
            lines = ["\nAlready tried and REJECTED by the gate "
                     "(do not repeat these, and do not re-propose a variation of one "
                     "without saying what is different):"]
            for entry in history:
                lines.append(f"  attempt {entry.get('attempt', '?')}: "
                             f"{entry.get('summary', 'no summary')}")
            parts.append("\n".join(lines) + "\n")

        parts.append(
            "\nReturn the complete corrected design as ONE fenced ```json block. Preserve "
            "the user's intent and every valid part of the design. Do not return a patch, "
            "and do not claim that validation or a build passed; PhiCad will run the gate "
            "again after your reply.\n\n"
            "Machine feedback:\n"
            f"{json.dumps(feedback, indent=2, sort_keys=True)}\n\n"
            "Current complete design:\n"
            f"{json.dumps(design, indent=2, sort_keys=True)}\n"
            "[END AUTOMATIC PHICAD VALIDATION FEEDBACK]"
        )
        return "".join(parts)

    def _emit(self, sink: Optional[EventSink], event: WorkflowEvent) -> None:
        self._sequence += 1
        event = replace(event, session_id=self.session_id, sequence=self._sequence)
        if sink is None:
            return
        try:
            sink(event)
        except Exception:  # noqa: BLE001 - a host listener cannot break validation
            pass

    def _outcome(self, ok: bool, design: dict, report: Mapping[str, Any],
                 repairs: int, last_reply: str, failure: str = "") -> ValidationOutcome:
        return ValidationOutcome(
            ok=ok,
            design=copy.deepcopy(design),
            report=dict(report),
            repairs=repairs,
            last_reply=last_reply,
            failure=failure,
        )

    def validate(self, design: Mapping[str, Any], on_event: Optional[EventSink] = None,
                 initial_feedback: Optional[Mapping[str, Any]] = None) -> ValidationOutcome:
        """Run the bounded automatic repair cycle.

        ``initial_feedback`` is used when a later build gate fails: the first model turn
        repairs that diagnostic, then the ordinary synth check loop proves the revision.
        The host must require fresh human approval before building the revised design.
        """
        try:
            return self._validate(design, on_event, initial_feedback)
        finally:
            # The session outlives this workflow and is reused for ordinary /synth
            # commands.  Leaving the correlation set would file every later run as one
            # more attempt of a cycle that finished.
            self.session.telemetry_run_id = None
            self.session.telemetry_attempt = None

    def _validate(self, design: Mapping[str, Any], on_event: Optional[EventSink] = None,
                  initial_feedback: Optional[Mapping[str, Any]] = None) -> ValidationOutcome:
        current = copy.deepcopy(dict(design))
        feedback = dict(initial_feedback) if initial_feedback is not None else None
        repairs = 0
        last_reply = ""
        last_report: Mapping[str, Any] = feedback or {}
        # What the gate has already rejected, so a later attempt is not allowed to look
        # like the first one to the model.  See repair_prompt().
        history: list[dict[str, Any]] = []

        if feedback is not None:
            self._emit(on_event, WorkflowEvent(
                kind="feedback_received",
                message="A failed build gate was queued for automatic repair.",
                max_attempts=self.max_repairs,
                data={"report": feedback},
            ))

        while True:
            if feedback is None:
                self._emit(on_event, WorkflowEvent(
                    kind="checking",
                    message="Checking the design against PhiCad's synth rules…",
                    attempt=repairs,
                    max_attempts=self.max_repairs,
                ))
                try:
                    self.session.save(current)
                    # Tag the synth runs this loop causes with the workflow that caused
                    # them, so a design that self-repairs reads as one cycle of N attempts
                    # rather than N-1 independent failures.
                    self.session.telemetry_run_id = self.session_id
                    self.session.telemetry_attempt = repairs
                    report = self.session.check_report()
                    if not isinstance(report, dict):
                        raise DesignError("synth check returned a non-object report")
                except Exception as exc:  # noqa: BLE001
                    failure = f"Synth checker could not run: {type(exc).__name__}: {exc}"
                    report = _error_report("synth-runtime", failure)
                    self._emit(on_event, WorkflowEvent(
                        kind="check_error",
                        message=failure,
                        attempt=repairs,
                        max_attempts=self.max_repairs,
                        data={"report": report},
                    ))
                    return self._outcome(
                        False, current, report, repairs, last_reply, failure)

                last_report = report
                if report.get("ok") is True:
                    self._emit(on_event, WorkflowEvent(
                        kind="validated",
                        message=("Design passed synth validation."
                                 if repairs == 0 else
                                 "Design passed after "
                                 f"{_count(repairs, 'automatic repair')}."),
                        attempt=repairs,
                        max_attempts=self.max_repairs,
                        data={
                            "report": report,
                            "design": copy.deepcopy(current),
                            "design_digest": design_digest(current),
                        },
                    ))
                    return self._outcome(True, current, report, repairs, last_reply)

                feedback = report
                count = _problem_count(report)
                self._emit(on_event, WorkflowEvent(
                    kind="validation_failed",
                    message=f"Synth found {_count(count, 'blocking problem')}.",
                    attempt=repairs,
                    max_attempts=self.max_repairs,
                    data={"report": report},
                ))

            if self.model is None:
                failure = "Automatic repair needs a configured model adapter."
                self._emit(on_event, WorkflowEvent(
                    kind="repair_unavailable",
                    message=failure,
                    attempt=repairs,
                    max_attempts=self.max_repairs,
                    data={"report": feedback},
                ))
                return self._outcome(False, current, last_report, repairs, last_reply, failure)

            if repairs >= self.max_repairs:
                failure = ("Automatic repair stopped after "
                           f"{_count(repairs, 'attempt')}.")
                self._emit(on_event, WorkflowEvent(
                    kind="repair_exhausted",
                    message=failure,
                    attempt=repairs,
                    max_attempts=self.max_repairs,
                    data={"report": feedback},
                ))
                return self._outcome(False, current, last_report, repairs, last_reply, failure)

            repairs += 1
            self._emit(on_event, WorkflowEvent(
                kind="repair_started",
                message=f"Sending the machine diagnostics to the model ({repairs}/{self.max_repairs})…",
                attempt=repairs,
                max_attempts=self.max_repairs,
                data={"report": feedback},
            ))
            prompt = self.repair_prompt(current, feedback, repairs, self.max_repairs,
                                        history=history)
            if feedback:
                history.append({"attempt": repairs,
                                "summary": _rejection_summary(feedback)})
            try:
                reply = self.model.ask(prompt)
            except Exception as exc:  # noqa: BLE001
                failure = f"Model repair turn failed: {type(exc).__name__}: {exc}"
                report = _error_report("model-adapter", failure)
                self._emit(on_event, WorkflowEvent(
                    kind="repair_error",
                    message=failure,
                    attempt=repairs,
                    max_attempts=self.max_repairs,
                    data={"report": report},
                ))
                return self._outcome(False, current, report, repairs, last_reply, failure)

            last_reply = reply
            corrected = extract_design(reply)
            if corrected is None:
                feedback = _error_report(
                    "model-response",
                    "The repair reply did not contain a complete design JSON object.",
                    "Return one full object containing both 'parts' and 'nets'.",
                )
                last_report = feedback
                self._emit(on_event, WorkflowEvent(
                    kind="repair_rejected",
                    message="The repair reply had no complete design; asking again automatically.",
                    attempt=repairs,
                    max_attempts=self.max_repairs,
                    data={"report": feedback},
                ))
                continue

            if json.dumps(corrected, sort_keys=True) == json.dumps(current, sort_keys=True):
                feedback = _error_report(
                    "model-response",
                    "The repair reply returned the design unchanged.",
                    "Change the fields named by the most recent machine diagnostics.",
                )
                last_report = feedback
                self._emit(on_event, WorkflowEvent(
                    kind="repair_rejected",
                    message="The model returned an unchanged design; asking again automatically.",
                    attempt=repairs,
                    max_attempts=self.max_repairs,
                    data={"report": feedback},
                ))
                continue

            current = corrected
            feedback = None
            self._emit(on_event, WorkflowEvent(
                kind="design_updated",
                message="The model supplied a corrected design; checking it now.",
                attempt=repairs,
                max_attempts=self.max_repairs,
                data={
                    "design": copy.deepcopy(current),
                    "design_digest": design_digest(current),
                },
            ))
