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
"""Talk to a local AI CLI (`phi`) from the AI View window.

The backend is a plain subprocess: one `phi --print` invocation per user turn, with
conversation continuity provided by `--session-id <uuid>` (verified: phi replays the
whole session on every call, so turn N sees turns 1..N-1).

DESIGN NOTES

  * NO secrets here.  `phi` owns its own credentials; we never see or pass a key.
  * Resolution is NOT configurable.  The harness is phi, baked in:
      1. `phi` on $PATH
      2. ~/.local/bin/phi        (KiCad.app inherits a Finder PATH that misses it)
    If neither resolves, resolve_backend() returns None and the GUI degrades gracefully
    — it never substitutes another CLI.

    $PHICAD_AI_CMD, $PHICAD_AI_BACKEND, $PHICAD_AI_MODEL and $PHICAD_CODEX_HOME used to
    steer this and are now INERT (see resolve_backend and ignored_ai_overrides).  An env
    var that redirects the assistant is reachable by anything touching the launching
    environment, and whatever it names receives the board context and the user's prompts.
    The model is not pinned either: the live phi session owns it, and the panel follows.
  * Proxy env (HTTPS_PROXY etc.) is stripped from the child: on managed machines it
    points at an LLM gateway that breaks ordinary HTTP clients, and phi manages its
    own network configuration.
  * `--no-tools --no-extensions --no-skills --no-prompt-templates --no-context-files`
    keeps the assistant a pure chat turn: no file reads, no shell, no AGENTS.md pickup
    from whatever directory KiCad launched us in. One explicit bundled extension is still
    loaded (explicit `--extension` survives `--no-extensions`): it adds only LiteLLM's
    `client=phicad` spend tag, never a credential or model setting.
  * Turns run as `--mode json`: stdout is a stream of one-JSON-object-per-line events,
    from which we take the assistant text, the model that actually answered, and the
    real token/cost usage the provider reported (see TurnUsage).  A backend that does
    not know `--mode json` is detected once and the session falls back to plain text
    for its remaining turns — replies still work, usage is simply not recorded.
  * stderr carries phi's startup chatter (MCP probes etc.) and is only surfaced on
    failure.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from pylib_path import add_pylib

    add_pylib()
    import telemetry
except Exception:                                    # pragma: no cover - never fatal
    telemetry = None

DEFAULT_TIMEOUT_S = 300.0
PHI_CLIENT_TAG_EXTENSION = Path(__file__).resolve().with_name("phi_client_tag.mjs")

_PROXY_VARS = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)

SYSTEM_PROMPT = """\
You are the PhiCad AI View assistant, embedded in a fork of KiCad.
You answer questions about the printed circuit board the user has open in the PCB editor.

Some user messages begin with a block delimited by
[LIVE BOARD CONTEXT] ... [END LIVE BOARD CONTEXT] - that block was read from the running
editor over the IPC API moments before the message was sent. Treat it as ground truth
about the current board. Dimensions are millimetres unless stated otherwise.

Rules:
- Base answers about the board strictly on the context block. If it does not contain
  what you need, say exactly that instead of guessing.
- The "connectivity heuristics" in the context are client-side approximations, NOT a
  DRC run; caveat them accordingly.
- You cannot edit the board yourself, and you cannot run DRC or click anything. But the
  pane you are running in DOES accept write commands typed by the user, and you should
  recommend the exact command rather than describing menus:

      /move   <REF> <dx> <dy>     move by dx,dy mm
      /place  <REF> <x> <y>       move to an absolute position, mm
      /rotate <REF> <deg>         set orientation
      /turn   <REF> <deg>         add to the current orientation
      /value  <REF> <text>        set the Value field
      /save                       write the board to disk

  When asked to change something, give the command on its own line so the user can copy
  it, say what it will do, and note that nothing is written to disk until /save. Only
  suggest /save when the user asks to save. If a change needs anything beyond these
  commands (tracks, nets, footprint swaps), say so plainly and describe the editor route.
- Never claim you have applied a change. You have not; the user runs the command.
- Be concise and concrete. Refer to nets and components by the names and reference
  designators in the context.

THE WINDOW YOU LIVE IN, AND THE VIEW YOU CAN MOVE

The pane draws its OWN picture of the board - PCB, Schematic and 3D tabs rendered by
kicad-cli - and it has two modes:

  Agent view   the render fills a maximised window and this chat is a small panel
               floating over it, which the user can close and reopen
  Manual       the original split: board views on the left, this chat on the right,
               and the user works in the real PhiCad editor

You can recommend commands that move THAT view. You cannot move the PCB editor's own
canvas: the IPC API has no zoom, pan or scroll command at all, so never claim to have
scrolled or zoomed what the user is looking at in PhiCad itself.

  /view pcb | sch | 3d | context      show that tab
  /view layers front|back|both        which copper set the PCB tab plots
  /view side top|bottom|front|left    which angle the 3D tab renders
  /view zoom in|out|fit|<factor>      zoom the visible tab
  /view render                        re-render the visible tab from the board file
  /view                               report what is on screen right now
  /mode agent | manual                switch mode
  /chat open | close                  show or hide the floating chat panel
  /editor raise                       bring the PhiCad editor window to the front

Same contract as the write commands: you recommend, the user runs it, and the pane
reports what the window ACTUALLY ended up showing. A slash command is intercepted before
it reaches you, so you never see one and cannot run one yourself.

When an answer is about something visible - a footprint on the back, a component's
height, silkscreen overlap - offer the view command that would show it, on its own line,
rather than describing where to click.

DESIGNING A NEW BOARD

The pane can also compile a whole new project from a design description. When the user
asks for a board that does not exist yet, answer with ONE JSON object in a single fenced
```json block - the design IR - and nothing else in that block. The user then runs:

    /design save     keep your JSON
    /synth check     validate it (milliseconds)
    /synth build     scaffold, netlist, place, route, silkscreen, DRC

The IR, with only the keys you need:

```json
{
  "project": "sensor-node",
  "title": "I2C sensor node",
  "board": {"layers": 2, "outline": "auto", "margin": 2.0,
            "corner": {"chamfer": 2.0}, "ground_net": "GND", "pour_ground": true},
  "rules": {"clearance": 0.2, "track_width": 0.25,
            "track_widths": {"3V3": 0.4, "GND": 0.4}},
  "parts": {
    "J1": {"footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
           "value": "I2C+PWR"},
    "U1": {"footprint": "Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering",
           "value": "BME280", "lcsc": "C92489"}
  },
  "nets": {"GND": ["J1.1", "U1.1", "U1.7"], "3V3": ["J1.2", "U1.6", "U1.8"]},
  "no_connect": [],
  "placement": {"J1": {"edge": "west"}, "C1": {"near": "U1", "within": 2.5}}
}
```

Rules for the design, all of them things the compiler will otherwise reject:

- EVERY pad of every part must appear in exactly one net or in "no_connect". This is the
  connectivity gate; a pad you forgot is an error, not a warning. Count the footprint's
  pads before you write the nets.
- Pins are written "REF.PAD" using the FOOTPRINT's pad names, which are not always the
  chip's pin names. If you are unsure what pads a footprint has, say so and ask rather
  than guessing - a wrong pad number fails the gate with the real list in the message.
- Footprints are "Library:Name" from the stock KiCad libraries. Use ones you are sure
  exist; a wrong name fails with the closest real matches listed.
- You do not choose coordinates. Express intent instead: "near", "within", "edge",
  "group", and let the placer resolve it. Only use "at": [x, y] when something is fixed
  by the outside world, like a connector aligned to an enclosure cutout.
- Give the design real part values and, where you know them, mpn/lcsc.

Say in one or two sentences what the circuit does and what you assumed, then the block.
Do not claim the board is built - the user runs /synth build, and it reports every gate."""


class ChatError(RuntimeError):
    """The backend ran but did not produce a usable reply."""


@dataclass
class Backend:
    """A resolved, runnable AI CLI.

    Two flavors are spoken, distinguished by how the turn is framed, never by
    what the model is called (the capability rule from integration/README.md):

    * ``phi``   — `phi --print --mode json`, sessions via --session-id, usage
      and model identity echoed per turn in the event stream.
    * ``codex`` — `codex exec --json`, sessions via thread ids the CLI mints
      (`exec resume <id>`), token usage reported per turn, **no cost and no
      model echo** — the requested model is recorded instead, which is sound
      because codex fails the turn outright on a model it cannot serve.

    This is how a model reachable only through the Codex CLI today (GPT-6
    early access) becomes usable everywhere before the gateway serves it.
    """

    argv: list[str]                      # base command, e.g. ["/Users/x/.local/bin/phi"]
    source: str                          # how it was found (for the status line)
    version: str = ""                    # filled lazily by probe_version()
    json_mode: bool = True               # cleared once if the CLI rejects --mode json
    flavor: str = "phi"                  # "phi" | "codex": which CLI dialect argv speaks
    model: str = ""                      # model to request (-m); "" = the CLI's default

    def display(self) -> str:
        exe = self.argv[0]
        home = os.path.expanduser("~")
        if exe.startswith(home):
            exe = "~" + exe[len(home):]
        ver = f" {self.version}" if self.version else ""
        mdl = f" · {self.model}" if self.model else ""
        return f"{os.path.basename(self.argv[0])}{ver}{mdl} ({exe}, via {self.source})"


@dataclass(frozen=True)
class TurnUsage:
    """What one model turn actually was and cost, as the provider reported it.

    Every field is copied verbatim from the backend's own usage report — nothing
    here is estimated, scaled, or derived from byte counts.  `model`/`provider`
    are the identity of the model that produced THIS reply (the user can switch
    models mid-session in phi, so the session's configuration is not an
    answer); capability decisions belong to the negotiated handshake, not here.
    """

    model: str = ""
    provider: str = ""
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    tokens_reasoning: Optional[int] = None
    cache_read: Optional[int] = None
    cost_usd: Optional[float] = None

    def telemetry_fields(self) -> dict:
        """Non-None fields under the names phicad/pylib/telemetry.py records."""
        fields = {
            "model": self.model or None,
            "provider": self.provider or None,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_reasoning": self.tokens_reasoning,
            "cache_read_tokens": self.cache_read,
            "cost_usd": self.cost_usd,
        }
        return {k: v for k, v in fields.items() if v is not None}


def parse_print_events(stdout: str) -> tuple[str, Optional[TurnUsage]]:
    """The assistant text and usage from a `phi --print --mode json` event stream.

    Concatenates the text content of every assistant `message_end` in the turn
    (there is normally exactly one — tools are disabled) and takes identity and
    usage from the last one.  Returns ("", None) when the stream contains no
    assistant message — callers treat that exactly like an empty reply.
    """
    texts: list[str] = []
    usage: Optional[TurnUsage] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text") or "")
        raw = message.get("usage") or {}
        cost = raw.get("cost") or {}
        usage = TurnUsage(
            model=str(message.get("model") or ""),
            provider=str(message.get("provider") or ""),
            tokens_in=raw.get("input"),
            tokens_out=raw.get("output"),
            tokens_reasoning=raw.get("reasoning"),
            cache_read=raw.get("cacheRead"),
            cost_usd=cost.get("total"),
        )
    return "\n".join(t for t in texts if t).strip(), usage


def parse_codex_events(stdout: str) -> tuple[str, Optional[TurnUsage], str]:
    """Reply text, usage, and thread id from a `codex exec --json` event stream.

    codex 0.153 shape, captured live: `thread.started` carries the thread id
    (the session handle for `exec resume`), `item.completed` items of type
    `agent_message` carry the reply, and `turn.completed` carries token usage.
    Usage is copied verbatim; codex reports no dollar cost (ChatGPT-plan auth),
    so none is recorded — absent means unmeasured, never free.  Identity is NOT
    in the stream; the caller records the requested model (see Backend).
    """
    texts: list[str] = []
    usage: Optional[TurnUsage] = None
    thread_id = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        etype = event.get("type")
        if etype == "thread.started":
            thread_id = str(event.get("thread_id") or "")
        elif etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                texts.append(item.get("text") or "")
        elif etype == "turn.completed":
            raw = event.get("usage") or {}
            usage = TurnUsage(
                tokens_in=raw.get("input_tokens"),
                tokens_out=raw.get("output_tokens"),
                tokens_reasoning=raw.get("reasoning_output_tokens"),
                cache_read=raw.get("cached_input_tokens"),
                cost_usd=None,
            )
    return "\n".join(t for t in texts if t).strip(), usage, thread_id


def turn_argv(backend: Backend, *, session_ref: str, system_prompt: str,
              prompt: str, codex_thread: str = "",
              schema_file: Optional[str] = None) -> list[str]:
    """The exact command line for one turn, in the backend's own dialect.

    Shared by the chat pane and the design loop so a new backend flavor is
    taught in exactly one place.  For codex: sessions resume by thread id;
    there is no system-prompt flag, so the directives ride visibly at the top
    of the FIRST prompt of a thread (resumed turns already carry them in
    context); `--output-schema` binds the final message to a JSON schema — the
    structured-output capability phi does not have.
    """
    if backend.flavor == "codex":
        argv = list(backend.argv) + ["exec"]
        if codex_thread:
            # `exec resume` speaks a narrower dialect: no --color / --sandbox
            # (the thread keeps the policy it was created with).
            argv += ["resume", codex_thread, "--json", "--skip-git-repo-check"]
        else:
            argv += ["--json", "--skip-git-repo-check", "--color", "never",
                     "--sandbox", "read-only"]
        if backend.model:
            argv += ["--model", backend.model]
        if schema_file:
            argv += ["--output-schema", schema_file]
        if not codex_thread and system_prompt:
            prompt = ("[SYSTEM DIRECTIVES]\n" + system_prompt
                      + "\n[END SYSTEM DIRECTIVES]\n\n" + prompt)
        return argv + [prompt]

    return list(backend.argv) + [
        "--print",
        *(["--model", backend.model] if backend.model else []),
        *(["--mode", "json"] if backend.json_mode else []),
        "--no-tools",
        # Disable discovery, then load exactly one reviewed extension. It adds the standard
        # LiteLLM client tag so the per-user key still supports per-app spend slices. It does
        # not receive or transport a credential; phi owns that end to end.
        "--no-extensions",
        "--extension", str(PHI_CLIENT_TAG_EXTENSION),
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--session-id", session_ref,
        "--system-prompt", system_prompt,
        prompt,
    ]


def _rejected_json_mode(returncode: int, stderr: str) -> bool:
    """Did this failure mean 'the CLI does not know --mode json'?"""
    if returncode <= 0:
        return False
    err = (stderr or "").lower()
    return "--mode" in err and ("unknown" in err or "unrecognized" in err
                                or "invalid" in err or "unexpected" in err)


def resolve_backend() -> Backend | None:
    """Resolve THE harness.  phi, or nothing.  None = degrade gracefully.

    There is deliberately no configuration here. PhiCad's AI harness is phi, and a release
    build must not be able to talk to anything else, so this function takes no environment
    input and offers no fallback:

      * `$PHICAD_AI_CMD` / `$PHICAD_AI_BACKEND` / `$PHICAD_AI_MODEL` are IGNORED. They are
        read only to warn that they were ignored (see `ignored_ai_overrides()`), never to
        change behaviour. An env var that silently redirects the assistant is a supply-chain
        hole: anything that can set it in the launching environment -- a shell profile, a
        stale Finder session, a `.env`, a hostile project directory -- gets to choose which
        binary receives the board context and the user's prompts.
      * There is no Codex fallback. "Fall back to whatever is on PATH" means a machine
        missing phi quietly gets a different vendor, different data handling and a different
        (unbudgeted, unaudited) credential. Reporting "phi is not installed" is strictly more
        useful than answering from an unknown engine.

    The model is NOT pinned here either: the live phi session owns its model, so the panel
    follows whatever the user selected in phi rather than overriding it (the session's model is
    read, not dictated).

    Locked because Phase 1 ships this to other people. `phicad doctor` reports which path
    resolved, so a missing phi is diagnosable without a knob to "fix" it with.
    """
    return _find_phi()


#: Variables that USED to steer the assistant and are now inert. Kept as a named list so the
#: GUI, `doctor` and the tests all warn about exactly the same set instead of drifting apart.
IGNORED_AI_ENV = ("PHICAD_AI_CMD", "PHICAD_AI_BACKEND", "PHICAD_AI_MODEL", "PHICAD_CODEX_HOME")


def ignored_ai_overrides(env: dict | None = None) -> list[str]:
    """Which inert AI overrides are set in this environment.

    Silence would be wrong in both directions: a user who set `PHICAD_AI_MODEL` deserves to
    know it did nothing, and a reviewer deserves to see that the value could not have taken
    effect. Returned rather than printed so each caller can surface it in its own idiom.
    """
    src = os.environ if env is None else env
    return [k for k in IGNORED_AI_ENV if (src.get(k) or "").strip()]


def _find_phi() -> Backend | None:
    exe = shutil.which("phi")
    source = "$PATH"
    if not exe:
        fallback = os.path.expanduser("~/.local/bin/phi")
        if not os.access(fallback, os.X_OK):
            return None
        exe, source = fallback, "default install path"
    # No model is pinned: the live phi session owns its model and the panel follows it.
    return Backend(argv=[exe], source=source)


def _find_codex(model: str = "", source: str = "$PATH") -> Backend | None:
    """Locate the Codex CLI.  NOT REACHABLE from resolve_backend() any more.

    Deliberately left in place rather than deleted. The codex dialect is threaded through
    turn_argv, the event parser and design_agent (~100 references); tearing that out during a
    release would be a large, untested refactor for no behavioural gain, and `flavor` is what
    makes the dialect boundary legible. What matters for the release is that nothing can SELECT
    it: resolve_backend() calls _find_phi() only, and a test asserts that a machine with codex
    installed and every override set still resolves to phi or to nothing.

    Do not wire this back into resolve_backend(). If PhiCad ever adopts a second harness it
    should be an explicit, reviewed decision -- not an automatic fallback that fires on the
    machines where phi happens to be missing.
    """
    exe = shutil.which("codex") or next(
        (p for p in ("/opt/homebrew/bin/codex", os.path.expanduser("~/.local/bin/codex"))
         if os.access(p, os.X_OK)), None)
    if not exe:
        return None
    return Backend(argv=[exe], source=source, flavor="codex", model=model)


def child_env() -> dict[str, str]:
    """Environment for the phi child, with LLM proxy variables removed.

    No PhiCad variable redirects a credential, model or harness. The former AI overrides are
    removed as well as ignored so a nested integration cannot accidentally resurrect them.
    """
    env = dict(os.environ)
    for var in (*_PROXY_VARS, *IGNORED_AI_ENV):
        env.pop(var, None)
    return env


def _child_env() -> dict[str, str]:
    """Compatibility alias for existing callers; new adapters use ``child_env``."""
    return child_env()


def probe_version(backend: Backend, timeout: float = 15.0) -> str:
    """Fill backend.version via `<cmd> --version`.  Never raises."""
    try:
        proc = subprocess.run(backend.argv + ["--version"], capture_output=True,
                              text=True, timeout=timeout, env=_child_env())
        if proc.returncode == 0 and proc.stdout.strip():
            backend.version = proc.stdout.strip().splitlines()[0][:40]
    except Exception:
        pass
    return backend.version


@dataclass
class ChatSession:
    """One conversation: a fixed session id threaded through every phi call."""

    backend: Backend
    workdir: str | None = None
    timeout_s: float = field(default_factory=lambda: float(
        os.environ.get("PHICAD_AI_TIMEOUT", DEFAULT_TIMEOUT_S)))
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    turns: int = 0
    # Provider-reported usage, surfaced for the GUI's status line.  last_usage is
    # the previous turn's report (None when that backend reported nothing — e.g.
    # after the text-mode downgrade); cost_usd_total sums only reported costs, so
    # it is a floor, not an estimate: unmeasured turns add nothing rather than a
    # guess.  Same verbatim-copy rule as telemetry.
    last_usage: Optional[TurnUsage] = None
    cost_usd_total: float = 0.0
    # codex mints its own session handle on the first turn (thread.started);
    # later turns resume it.  Empty until then, and always empty for phi.
    codex_thread: str = ""

    @staticmethod
    def compose(message: str, context: str | None) -> str:
        """The exact text sent as the user turn (also used by the degraded-mode UI)."""
        if context:
            return (
                "[LIVE BOARD CONTEXT]\n"
                f"{context}\n"
                "[END LIVE BOARD CONTEXT]\n\n"
                f"{message}"
            )
        return message

    def ask(self, message: str, context: str | None = None, on_start=None) -> str:
        """One blocking turn.  Raises ChatError on any failure; never fabricates.

        `on_start(popen)` is handed the live child so the caller can stop it.  Popen
        rather than subprocess.run for exactly that reason: run() owns the process for
        its whole lifetime, so a turn that has gone wrong can only be waited out --
        which, on the 300 s default timeout, is a window that looks hung with no way
        back.  Cancelling is the caller's decision; this only makes it reachable.
        """
        prompt = self.compose(message, context)
        argv = turn_argv(self.backend, session_ref=self.session_id,
                         system_prompt=SYSTEM_PROMPT, prompt=prompt,
                         codex_thread=self.codex_thread)

        # Sizes, never content: the prompt is a circuit someone is designing and the
        # reply is the design.  See phicad/pylib/telemetry.py.  Token and cost fields
        # are recorded only as the provider reported them (`--mode json` usage); bytes
        # scaled by a guessed ratio would make every derived cost a fabrication, so a
        # text-mode fallback turn records sizes and no usage at all.
        started = time.monotonic()
        # Fixed now, not read at record time: the success path increments self.turns
        # before recording, which would number the turn it just finished as the next one.
        turn_no = self.turns + 1

        def record(outcome: str, **extra) -> None:
            if telemetry is None:
                return
            telemetry.emit(
                "ai.turn",
                session_id=self.session_id,
                turn=turn_no,
                backend=os.path.basename(self.backend.argv[0]),
                backend_version=self.backend.version or None,
                backend_source=self.backend.source,
                has_context=context is not None,
                context_bytes=len(context.encode("utf-8")) if context else 0,
                prompt_bytes=len(prompt.encode("utf-8")),
                timeout_s=self.timeout_s,
                duration_ms=round((time.monotonic() - started) * 1000),
                outcome=outcome,
                **extra)

        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    cwd=self.workdir, env=_child_env(),
                                    # codex blocks reading a piped stdin; close
                                    # it explicitly so a turn never waits on one.
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            record("launch_failed", error_type=type(exc).__name__)
            raise ChatError(f"could not launch {self.backend.argv[0]}: {exc}") from exc

        if on_start is not None:
            on_start(proc)
        try:
            out, err = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            record("timeout")
            raise ChatError(
                f"AI backend timed out after {self.timeout_s:.0f}s "
                f"(set PHICAD_AI_TIMEOUT to change)") from None

        if proc.returncode != 0:
            tail = "\n".join((err or "").strip().splitlines()[-6:])
            # A killed child reports a negative code, and "exited with code -9" reads as
            # a crash when it was the user pressing Stop.  Say which it was.
            if proc.returncode < 0:
                record("stopped", signal=-proc.returncode)
                raise ChatError(f"the turn was stopped (signal {-proc.returncode}); "
                                "nothing was written and the session is unchanged.")
            if self.backend.flavor == "phi" \
                    and self.backend.json_mode and _rejected_json_mode(proc.returncode, err):
                # Old CLI.  Downgrade the whole backend once and replay this turn in
                # text mode -- the session id makes the replay identical.
                self.backend.json_mode = False
                record("json_mode_unsupported")
                return self.ask(message, context, on_start)
            record("exit_nonzero", exit_code=proc.returncode)
            raise ChatError(
                f"AI backend exited with code {proc.returncode}."
                + (f"\nstderr tail:\n{tail}" if tail else ""))

        usage = None
        if self.backend.flavor == "codex":
            reply, usage, thread = parse_codex_events(out or "")
            if thread:
                self.codex_thread = thread
            if usage:
                # codex does not echo the serving model; the REQUESTED one is
                # recorded because codex fails the turn on a model it cannot
                # serve, so requested == served whenever a reply exists.  With
                # no request (CLI default model) the field stays honest-empty.
                usage = dataclasses.replace(usage, model=self.backend.model,
                                            provider="codex")
        elif self.backend.json_mode:
            reply, usage = parse_print_events(out or "")
        else:
            reply = (out or "").strip()
        if not reply:
            tail = "\n".join((err or "").strip().splitlines()[-6:])
            record("empty_reply")
            raise ChatError("AI backend returned an empty reply."
                            + (f"\nstderr tail:\n{tail}" if tail else ""))
        self.turns += 1
        self.last_usage = usage
        if usage and usage.cost_usd:
            self.cost_usd_total += usage.cost_usd
        record("ok", reply_bytes=len(reply.encode("utf-8")),
               **(usage.telemetry_fields() if usage else {}))
        return reply


if __name__ == "__main__":
    # Smoke test:  build/venv/bin/python chat_backend.py "your question"
    import sys

    backend = resolve_backend()
    if backend is None:
        print("no AI backend found (`phi` is not on PATH or at ~/.local/bin/phi)")
        sys.exit(1)
    probe_version(backend)
    print(f"backend: {backend.display()}")
    session = ChatSession(backend)
    print(session.ask(" ".join(sys.argv[1:]) or "Reply with exactly: BACKEND OK"))
