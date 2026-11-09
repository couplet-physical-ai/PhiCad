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
"""Newline-delimited JSON records of what the agent suite actually did.

One object per line, one file per day, under `~/.phicad/telemetry/`.  No server,
no dependencies, nothing to stand up: `cat` and `jq` work on it, and so does
`phicad telemetry`.

What is recorded, and what is deliberately not
----------------------------------------------
**Never the prompt or the reply.**  A prompt to this tool is a circuit someone is
designing; the reply is the design.  That is the most confidential thing on the
machine and it is not going in a log file that outlives the session.  Sizes are
recorded instead, which answers the questions a size can answer -- is context
blowing up, are replies being truncated -- without keeping the content.

**Never a derived token count.**  Token and cost fields (`tokens_in`,
`tokens_out`, `tokens_reasoning`, `cache_read_tokens`, `cost_usd`, with the
`model`/`provider` that produced them) are recorded only when the backend
reported them itself — `phi --print --mode json` emits per-turn usage, and the
AI View adapters copy it verbatim (see chat_backend.TurnUsage).  A turn through
a backend that reports nothing records bytes and no usage at all: a byte count
multiplied by a guessed ratio is not a token count, and putting one in a field
called `tokens` would make every cost figure derived from this file a
fabrication.  Absent field = not measured, exactly like everything else here.

Cost per design is therefore answerable by summing `cost_usd` over a session's
`ai.turn` records — `phicad telemetry` does it for you.

Failure policy
--------------
Telemetry never raises.  Every public function swallows its own errors, because a
full disk or a read-only home directory must not be able to end a design session.
The cost of that is silent loss, which is the right trade for a metrics file and
the wrong one for anything else -- so nothing else uses this.

Usage
-----
    from telemetry import emit, span

    emit("ai.turn", session_id=sid, outcome="ok", duration_ms=412)

    with span("synth.run", command="build") as rec:
        report = run_it()
        rec(stages_total=14, stages_passed=14)     # fields known only on success

`span` records `duration_ms` and an `outcome` of `ok` or `error` on its own; on an
exception it adds `error_type` and re-raises.  Opt out with `PHICAD_TELEMETRY=0`;
relocate with `PHICAD_TELEMETRY_DIR`.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

__all__ = ["emit", "span", "enabled", "log_dir", "current_log", "set_sink", "SCHEMA"]

# Bumped when a field changes meaning.  A reader that finds an unknown schema
# should say so rather than quietly mis-summarising an older file.
SCHEMA = 1

_DEFAULT_DIR = Path.home() / ".phicad" / "telemetry"
_sink: Optional[Callable[[Dict[str, Any]], None]] = None


def enabled() -> bool:
    """False when PHICAD_TELEMETRY is 0/false/no/off (case-insensitive)."""
    return os.environ.get("PHICAD_TELEMETRY", "1").strip().lower() not in (
        "0", "false", "no", "off")


def log_dir() -> Path:
    override = os.environ.get("PHICAD_TELEMETRY_DIR", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_DIR


def current_log() -> Path:
    """Today's file.  UTC, so a laptop crossing a timezone does not reorder days."""
    return log_dir() / f"{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"


def set_sink(sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Send records somewhere else as well as the file -- a forwarder, or a test.

    The sink receives the finished record.  It is called inside the same
    exception guard as the write, so a sink that raises loses its record and
    nothing more.  Pass None to remove.
    """
    global _sink
    _sink = sink


def emit(event: str, **fields: Any) -> None:
    """Append one record.  Never raises, never blocks on anything but the write."""
    if not enabled():
        return
    try:
        record: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "schema": SCHEMA,
            "event": event,
        }
        # A field whose value is None was not measured.  Recording it as null
        # invites a reader to average over it as though it were a zero.
        record.update({k: v for k, v in fields.items() if v is not None})

        path = current_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        # One open-append-close per record.  A long-lived handle would be faster
        # and would also lose the tail of the file whenever the GUI is killed,
        # which is exactly the run whose telemetry you wanted.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

        if _sink is not None:
            _sink(record)
    except Exception:
        pass


@contextmanager
def span(event: str, **fields: Any):
    """Time a block and record how it ended.

    Yields a callable that adds fields to the record being built, for everything
    that is only known once the work has succeeded.  On an exception the record
    still lands, with outcome="error" and the exception's class name, and the
    exception propagates untouched -- telemetry observes, it does not handle.
    """
    extra: Dict[str, Any] = {}

    def add(**more: Any) -> None:
        extra.update(more)

    started = time.monotonic()
    try:
        yield add
    except BaseException as exc:
        emit(event, duration_ms=round((time.monotonic() - started) * 1000),
             outcome="error", error_type=type(exc).__name__, **fields, **extra)
        raise
    else:
        merged = {**fields, **extra}
        merged.setdefault("outcome", "ok")
        emit(event, duration_ms=round((time.monotonic() - started) * 1000), **merged)
