# Copyright (C) 2026 Exowatt, Inc. / PhiCad contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, either version
# 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program.
# If not, see <http://www.gnu.org/licenses/>.

"""Describe a board in the chat pane; get a routed project back.

The shape of this, and why
--------------------------
`board_edits.py` lets the user edit the board that is open.  This module is the other half:
creating a board that does not exist yet.  It keeps the same contract, for the same reason —

    the MODEL proposes a design  ->  the USER commits it with a command  ->  we compile it
    ->  we report what every gate actually said

The model never writes a file and never starts a build.  It answers in the design IR, the
user reads it, and `/design save` is the moment a human agreed.  Human-in-the-loop stays
structural rather than a rule the model might not follow, and there is no output parsing
that could misfire into a filesystem write.

What makes this worth doing in the pane at all is that the design IR is small enough to
read.  A board used to arrive as a thousand lines of s-expressions, which nobody reviews;
sixty lines of parts and nets is something a person can actually check before agreeing to
it.

Everything here shells out to `phicad/synth/cli.py --json`.  The pane holds no synthesis
logic of its own, so the pane and the MCP tools and the command line cannot drift.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from pylib_path import add_pylib

    add_pylib()
    import telemetry
except Exception:                                    # pragma: no cover - never fatal
    telemetry = None

import project_history

# The plugin runs from wherever KiCad copied it, so the repo is found by env var or by
# walking up from this file when it is still inside a checkout.
_HERE = Path(os.path.dirname(os.path.realpath(__file__)))

BUILD_TIMEOUT_S = float(os.environ.get("PHICAD_SYNTH_TIMEOUT", "900"))

# A fenced ```json block, or any fenced block whose body starts with a brace.  Models are
# inconsistent about the language tag and consistent about the braces.
_FENCE = re.compile(r"```(?:json|jsonc|json5)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class DesignError(RuntimeError):
    """The command was rejected, or the design could not be compiled."""


def repo_root() -> Optional[Path]:
    """Where phicad/synth lives, or None if this plugin was installed outside a checkout."""
    explicit = os.environ.get("PHICAD_REPO", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if (candidate / "phicad" / "synth" / "cli.py").is_file():
            return candidate

    # An installed plugin is a real directory outside the repo, so walking up from here
    # only works when running straight out of a checkout.  `phicad ai-view-install` writes
    # the source checkout into `repo-path`; that is the authoritative answer.
    marker = _HERE / "repo-path"
    if marker.is_file():
        try:
            recorded = Path(marker.read_text(encoding="utf-8").strip())
        except OSError:
            recorded = None
        if recorded and (recorded / "phicad" / "synth" / "cli.py").is_file():
            return recorded

    for parent in [_HERE] + list(_HERE.parents):
        if (parent / "phicad" / "synth" / "cli.py").is_file():
            return parent

    # Last resort: the venv this copy was installed against usually sits inside a checkout.
    # Usually, not always -- the build tree is often in a different worktree from the source,
    # which is exactly why `repo-path` exists.
    venv_marker = _HERE / "venv-path"
    if venv_marker.is_file():
        try:
            venv = Path(venv_marker.read_text(encoding="utf-8").strip())
        except OSError:
            return None
        for parent in venv.parents:
            if (parent / "phicad" / "synth" / "cli.py").is_file():
                return parent
    return None


def python_for_synth(root: Path) -> str:
    """The interpreter that can run a build: it needs kipy, so it must be the venv.

    `synth check` would run on any python3, but offering two interpreters means the pane
    can validate a design it then cannot build, which is a worse failure than refusing up
    front.
    """
    explicit = os.environ.get("PHICAD_AI_PYTHON", "").strip()
    if explicit and os.access(explicit, os.X_OK):
        return explicit

    # The interpreter the installer vetted, recorded beside the plugin.  Preferred over
    # `root/build/venv` because the source checkout frequently has no build tree of its
    # own -- that is the normal worktree layout here, not an edge case.
    marker = _HERE / "venv-path"
    if marker.is_file():
        try:
            recorded = marker.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        if recorded and os.access(recorded, os.X_OK):
            return recorded

    venv = root / "build" / "venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return sys.executable


def extract_design(text: str) -> Optional[dict]:
    """The last JSON object in a model reply that looks like a design.

    The LAST one, not the first: a reply that shows a fragment and then the whole file
    means the whole file, and a reply that revises itself means the revision.
    """
    fenced: List[str] = [block.strip() for block in _FENCE.findall(text or "")]
    # Fenced blocks are the model following instructions and win outright; the
    # brace scan is for replies that lead with a prose line before naked JSON
    # ("I'll check the contract.\n\n{...") — GPT-6 via codex does this — where
    # "the whole reply starts with {" never matches.
    for block in list(reversed(fenced)) + list(reversed(_brace_spans(text or ""))):
        try:
            parsed = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "parts" in parsed and "nets" in parsed:
            return _strip_null_keys(parsed)
    return None


def _brace_spans(text: str) -> List[str]:
    """Top-level balanced {...} spans, string-literal aware.  Order preserved."""
    spans: List[str] = []
    depth, start = 0, -1
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start:i + 1])
                start = -1
    return spans


def _strip_null_keys(node):
    """Drop dict keys whose value is null, recursively.  Lists keep their shape.

    A schema-bound codex reply spells "this optional field is absent" as an
    explicit null (OpenAI strict mode requires every property present, so
    optionality becomes nullability — design_agent._codex_safe_schema).  The
    real design-IR contract wants such fields ABSENT; stripping here means one
    canonical form reaches validation no matter which backend produced it.
    """
    if isinstance(node, dict):
        return {k: _strip_null_keys(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_strip_null_keys(v) for v in node]
    return node


def format_check_report(report: dict) -> str:
    """Render a synth check report for a person without losing its JSON contract."""
    problems = report.get("problems", [])
    parts = report.get("parts")
    nets = report.get("nets")
    part_word = "part" if parts == 1 else "parts"
    net_word = "net" if nets == 1 else "nets"
    head = (f"{report.get('project')}: {parts} {part_word}, "
            f"{nets} {net_word}, {report.get('no_connect')} no-connect")
    if not problems:
        return head + "\n\nno problems — ready for /synth build."
    lines = [head, ""]
    for problem in problems:
        lines.append(
            f"{problem.get('severity', 'error')}: "
            f"{problem.get('where', 'design')}: {problem.get('message', 'invalid design')}"
        )
        if problem.get("hint"):
            lines.append(f"    {problem['hint']}")
    return "\n".join(lines)


@dataclass
class DesignSession:
    """The design the pane is currently working on, and where its project goes."""

    root: Optional[Path] = None
    workdir: Path = field(default_factory=lambda: Path(
        tempfile.mkdtemp(prefix="phicad-design-")))
    design_path: Optional[Path] = None
    project_dir: Optional[Path] = None
    last_board: Optional[Path] = None

    # Correlation for telemetry, set by whoever is driving a multi-run cycle.
    # DesignValidationWorkflow checks the same design once per repair attempt, so one
    # user action can produce nine synth runs, eight of them failing on the way to a
    # design that ends up valid.  Each record is true on its own; without a run id they
    # aggregate into "failed 8, ok 1" for something the user experienced as a success.
    telemetry_run_id: Optional[str] = None
    telemetry_attempt: Optional[int] = None

    def __post_init__(self) -> None:
        if self.root is None:
            self.root = repo_root()
        # The default workdir exists because mkdtemp created it; a CALLER-supplied
        # one may not, and without this the failure surfaces two steps later as
        # "Synth checker could not run: FileNotFoundError" — blaming the checker
        # for a directory save() never made.
        self.workdir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ file handling

    def save(self, payload: dict, name: Optional[str] = None) -> Path:
        project = str(payload.get("project") or name or "design").strip()
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", project) or "design"
        payload.setdefault("project", safe)
        path = self.workdir / f"{safe}.design.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self.design_path = path
        return path

    def require_design(self) -> Path:
        if self.design_path is None or not self.design_path.is_file():
            raise DesignError(
                "no design yet. Ask for a board in plain words, then run /design save to "
                "keep the JSON the assistant replied with.")
        return self.design_path

    def require_root(self) -> Path:
        if self.root is None:
            raise DesignError(
                "cannot find the PhiCad checkout from this plugin directory.\n"
                "Set PHICAD_REPO to the repository root and relaunch the window.")
        return self.root

    # -------------------------------------------------------------------- the pipeline

    def _run(self, argv: List[str], timeout: float,
             on_stage: Optional[Callable[[str], None]] = None) -> dict:
        root = self.require_root()
        cmd = [python_for_synth(root), str(root / "phicad" / "synth" / "cli.py")] + argv
        env = dict(os.environ)
        env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
        # The pane's own IPC socket belongs to the editor that launched it; a build opens
        # its own document-less server and must not be pointed at the user's session.
        env.pop("KICAD_API_SOCKET", None)
        env.pop("KICAD_API_TOKEN", None)
        # The subcommand ("check", "build") is argv[0]; the rest is paths, which are
        # not recorded -- a project path is as identifying as the design itself.
        started = time.monotonic()
        command = argv[0] if argv else "?"

        def record(outcome: str, **extra) -> None:
            if telemetry is None:
                return
            telemetry.emit("synth.run", command=command, streaming=on_stage is not None,
                           timeout_s=timeout,
                           run_id=self.telemetry_run_id,
                           attempt=self.telemetry_attempt,
                           duration_ms=round((time.monotonic() - started) * 1000),
                           outcome=outcome, **extra)

        if on_stage is None:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                      env=env, cwd=str(root))
            except subprocess.TimeoutExpired:
                record("timeout")
                raise DesignError(f"synth timed out after {timeout:.0f}s "
                                  "(set PHICAD_SYNTH_TIMEOUT to change)") from None
            except OSError as exc:
                record("launch_failed", error_type=type(exc).__name__)
                raise DesignError(f"could not run synth: {exc}") from None
            out, err = proc.stdout, proc.stderr
        else:
            # `--json` puts one object on stdout and a line per gate on stderr, so the
            # caller can have both: the report to act on, and real progress to show while a
            # build that takes tens of seconds is running.  Reading stderr as it arrives is
            # the only way to get the second one -- subprocess.run buffers until exit.
            try:
                out, err = _run_streaming(cmd, env, str(root), timeout, on_stage)
            except BaseException as exc:
                record("error", error_type=type(exc).__name__)
                raise

        try:
            report = json.loads(out or "")
        except (json.JSONDecodeError, ValueError):
            record("no_json")
            tail = "\n".join((err or out or "").strip().splitlines()[-8:])
            raise DesignError(
                f"synth produced no JSON report.\n{tail}") from None

        # Which gates ran and which failed, by name.  Stage names are fixed vocabulary
        # from the compiler, not user data, so recording them costs nothing and is the
        # difference between "a build failed" and "route fails on every four-layer board".
        stages = report.get("stages") or []
        failed = report.get("failed") or []
        record("ok" if report.get("ok") else "failed",
               stages_total=len(stages) or None,
               stages_passed=(len(stages) - len(failed)) if stages else None,
               failed_stages=failed or None)
        return report

    def check_report(self) -> dict:
        """Return the machine-readable gate result used by UIs and agent repair loops."""
        return self._run(["check", str(self.require_design()), "--json"], 120)

    def check(self) -> str:
        return format_check_report(self.check_report())

    def build_target(self, out_dir: Optional[str] = None) -> Path:
        """Where `build(out_dir)` will write.  Resolved here so the GUI can watch it.

        `--json` suppresses every stage line the compiler prints (cli.py `_say`), so the
        only honest progress a long build can offer is the artefacts appearing on disk.
        That needs the directory before the build starts, not after it returns.
        """
        design = self.require_design()
        return (Path(out_dir).expanduser() if out_dir
                else self.workdir / design.stem.replace(".design", ""))

    def build(self, out_dir: Optional[str] = None,
              on_stage: Optional[Callable[[str], None]] = None) -> str:
        design = self.require_design()
        target = self.build_target(out_dir)
        report = self._run(
            ["build", str(design), "--output", str(target), "--force", "--json"],
            BUILD_TIMEOUT_S, on_stage=on_stage)

        self.project_dir = target
        board = report.get("board")
        self.last_board = Path(board) if board else None

        lines = []
        for stage in report.get("stages", []):
            mark = "PASS" if stage.get("ok") else "FAIL"
            lines.append(f"{mark}  {stage.get('stage'):<13} {stage.get('detail', '')}")
        size = report.get("board_mm")
        if report.get("ok"):
            lines.append("")
            lines.append(f"Board: {board}")
            if size:
                lines.append(f"Size:  {size[0]:.1f} x {size[1]:.1f} mm")
            try:
                history = project_history.prepare_generated_project(target, design)
                lines.append(history["git"])
            except project_history.HistoryError as exc:
                lines.append(f"Git warning: {exc}")
            lines.append("Open it in PhiCad, or run /design open to load it in this editor.")
        else:
            lines.append("")
            lines.append("Stopped at: " + ", ".join(report.get("failed", []) or ["?"]))
            lines.append("Fix the design and run /synth build again — the gate that failed "
                         "names what it wanted.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# command parsing
# ---------------------------------------------------------------------------------------

# One table, three consumers: /help prints it, the entry box completes from it, and HELP
# is generated from it.  See view_commands.COMMANDS for the shape.
COMMANDS = (
    ("/design save", "[name]", "keep the design JSON from the assistant's last reply"),
    ("/design show", "", "print the design being worked on"),
    ("/design path", "", "where the design and its project live"),
    ("/design open", "", "open the built board in this PhiCad"),
    ("/synth check", "", "validate pads, nets and footprints (milliseconds)"),
    ("/synth build", "[dir]", "compile: scaffold, netlist, place, route, silk, DRC"),
)


def _help() -> str:
    body = "\n".join(f"  {name} {args}".ljust(38) + summary
                     for name, args, summary in COMMANDS)
    return (
        "Design commands (these create a NEW project; they never touch the open "
        "board):\n\n" + body + "\n\n"
        "Ask for a board in plain words first (\"a USB-C powered BME280 sensor node, "
        "I2C broken out to a 4-pin header\"). The assistant replies with the design; "
        "you read it; /design save is the point at which you agreed to it.")


HELP = _help()

# The stages `/synth build` runs, in order, with the artefact each one lands in the
# output directory.  The pane watches for these files so a long build can report real
# progress; see AiViewApp._watch_build.  A stage with no artefact is not listed --
# reporting one would be inventing progress, which is the one thing the pane may not do.
BUILD_ARTEFACTS = (
    ("scaffold", "{project}.kicad_pcb", "board scaffold written"),
    ("netlist", "{project}.net", "netlist written"),
    ("schematic", "{project}.kicad_sch", "schematic written"),
    ("erc", "erc.json", "ERC run"),
    ("drc", "drc.json", "DRC run"),
)

_PREFIXES = ("/design", "/synth")


def is_command(line: str) -> bool:
    first = line.strip().split(" ", 1)[0].lower()
    return first in _PREFIXES


def run_command(session: DesignSession, line: str, last_reply: str = "") -> str:
    """Execute one /design or /synth command.  Raises DesignError on bad input."""
    parts = line.strip().split()
    if not parts:
        raise DesignError("empty command")
    head, args = parts[0].lower(), parts[1:]
    sub = args[0].lower() if args else ""
    rest = args[1:]

    if head == "/design":
        if sub in ("", "help"):
            return HELP
        if sub == "save":
            payload = extract_design(last_reply)
            if payload is None:
                raise DesignError(
                    "the assistant's last reply has no design JSON in it.\n"
                    "Ask for the design as a single JSON block with 'parts' and 'nets', "
                    "then run /design save again.")
            path = session.save(payload, rest[0] if rest else None)
            return (f"saved {path}\n"
                    f"{len(payload.get('parts', {}))} parts, "
                    f"{len(payload.get('nets', {}))} nets. "
                    "Run /synth check next.")
        if sub == "show":
            # Fenced, so the pane renders it as a code block with the same syntax
            # colouring the assistant's proposal got.  The design the user is about to
            # build must not be harder to read than the one it was proposed from.
            path = session.require_design()
            return (f"{path}\n\n```json\n"
                    + path.read_text(encoding="utf-8").rstrip() + "\n```")
        if sub == "path":
            return "\n".join([
                f"workdir: {session.workdir}",
                f"design:  {session.design_path or '(none yet)'}",
                f"project: {session.project_dir or '(not built)'}",
                f"board:   {session.last_board or '(not built)'}",
            ])
        if sub == "open":
            if session.last_board is None or not session.last_board.is_file():
                raise DesignError("nothing built yet — run /synth build first.")
            return f"OPEN:{session.last_board}"
        raise DesignError(f"unknown /design subcommand {sub!r}. {HELP}")

    if head == "/synth":
        if sub in ("", "help"):
            return HELP
        if sub == "check":
            return session.check()
        if sub == "build":
            return session.build(rest[0] if rest else None)
        raise DesignError(f"unknown /synth subcommand {sub!r}. {HELP}")

    raise DesignError(f"unknown command {head!r}")


def _run_streaming(cmd, env, cwd: str, timeout: float,
                   on_stage) -> Tuple[str, str]:
    """Run synth, handing each stderr line to `on_stage` as it arrives.

    stdout is collected whole because it is one JSON object; stderr is consumed line by
    line on a reader thread, because the point is to see a gate the moment it reports
    rather than after the build ends.
    """
    import threading

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, cwd=cwd, bufsize=1)
    except OSError as exc:
        raise DesignError(f"could not run synth: {exc}") from None

    lines: List[str] = []

    def pump() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip("\n")
            lines.append(line)
            if line.strip():
                try:
                    on_stage(line)
                except Exception:      # noqa: BLE001 - a broken UI callback must not
                    pass               # take the build down with it
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    # Read stdout here rather than through communicate(): communicate() drains BOTH pipes,
    # so it races the reader thread for stderr and silently eats whichever lines it wins.
    # That cost two of the fourteen stage lines on the first run -- progress that vanishes
    # is worse than no progress, because it looks like the stage never ran.
    deadline = time.time() + timeout
    try:
        assert proc.stdout is not None
        out = proc.stdout.read()
    except OSError as exc:
        proc.kill()
        raise DesignError(f"synth failed while reading its report: {exc}") from None

    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        proc.kill()
        proc.wait()
        raise DesignError(f"synth timed out after {timeout:.0f}s "
                          "(set PHICAD_SYNTH_TIMEOUT to change)")

    reader.join(timeout=2)
    return out or "", "\n".join(lines)
