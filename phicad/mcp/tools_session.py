#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Total read/write control of the LIVE headless PhiCad session, as MCP tools.

WHAT THIS EXPOSES (23 plain functions; the server decorates them with @mcp.tool)
    READ    session_list_items  session_get_item  session_get_nets  session_get_stackup
            session_bounding_box  session_hit_test  session_selection  session_title_block
    WRITE   session_update_footprint  session_batch_move  session_add_track  session_add_via
            session_add_zone  session_add_text  session_add_graphic  session_delete_items
            session_delete_by_filter  session_set_net  session_set_enabled_layers
            session_set_origin  session_refill_zones  session_run_action  session_revert

    The board-level session lifecycle (open/save/close/query) lives in server.py; this module
    is the item-level surface on top of the same managed api-server.

WHY A WORKER SUBPROCESS
    This module runs in the MCP venv, which has fastmcp and NO kipy, on purpose: a protobuf
    version skew inside the server process would take the whole tool surface down.  Every call
    shells out to `phicad/mcp/session_worker.py` under the AUTOMATION venv (which carries kipy
    0.8.0 built against the fork's regenerated protos) with KICAD_API_SOCKET pointing at the
    managed server's socket, and parses the one JSON line it prints.

WHY NOT ctx['session']
    The server's own `session` helper runs server.py's embedded snippet, which only knows
    open/documents/save/close/query/edit.  These tools need their own worker, so they build the
    argv themselves with ctx['run'] — same env, same socket, different script.

UNITS AND IDS
    Millimetres at the tool boundary, nanometres inside the worker.  Items are addressed by
    KIID string (what `session_list_items` returns as `id`), footprints additionally by
    reference designator.

WHAT `ok` MEANS ON A WRITE
    The RPC landed AND a fresh read of the board proves it: every writer re-reads through a
    new kicad.get_board() and reports `applied` from that comparison, so `ok: true` is
    evidence, not optimism.  Multi-item writes are wrapped in begin_commit/push_commit and
    dropped on failure, so they are one undo step and never half-applied.

NOTHING IS ON DISK
    These tools mutate the SESSION.  Every write returns `outputs: []`, `mutated: []` and
    `saved: false` — the board file is only touched when you call session_save().
"""

from __future__ import annotations

import inspect
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------------------
# Injected context (configure() fills these in; every tool refuses politely until then)
# --------------------------------------------------------------------------------------

KICAD_CLI: Path | None = None
VENV_PY: Path | None = None
REPO_ROOT: Path | None = None
MCP_DIR: Path | None = None

_run: Callable[..., dict[str, Any]] | None = None
_cli_env: Callable[[], dict[str, str]] | None = None
_api_alive: Callable[[], bool] | None = None
_api: dict[str, Any] | None = None

#: Reads are cheap; hit_test and get_nets(with_items) fan out to one RPC per item, and a
#: zone refill on a big board is genuinely slow, so the ceiling is generous.
WORKER_TIMEOUT_S = 300
REFILL_TIMEOUT_S = 900

_REQUIRED = ("VENV_PY", "REPO_ROOT", "run", "cli_env", "api_alive", "api")

# --------------------------------------------------------------------------------------
# The precondition sentence, stamped onto every session tool's description
#
# A chat client shows a model the tool description and nothing else, so a tool whose docstring
# never mentions api_server_start()/session_open() reads as if it works on a cold server -- and
# an agent's FIRST live-edit attempt then fails on a precondition it was never told about.
# Hand-editing every docstring rots the moment a tool is added, so the sentence is appended
# programmatically in configure(): a new tool in TOOLS gets it for free and cannot drift.
# server.py reads SESSION_PRECONDITION from here for its own session_* tools, so the wording
# has exactly one source.
# --------------------------------------------------------------------------------------

#: Appended (as its own paragraph) to every ITEM-level session tool by configure(): these need
#: both the managed server and an open document.
SESSION_PRECONDITION = (
    "PRECONDITION: this needs a live headless session — call api_server_start() once, then "
    "session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it "
    "returns ok=false with a hint naming the missing step."
)

#: For the lifecycle tools that only need the managed server (server.py stamps its own with
#: this one — telling session_open to call session_open would be nonsense).
API_SERVER_PRECONDITION = (
    "PRECONDITION: this needs the managed api-server — call api_server_start() (the file "
    "argument is optional) first; without it it returns ok=false with that hint."
)


def stamp_precondition(fns: Any, sentence: str = SESSION_PRECONDITION) -> list[str]:
    """Append a precondition sentence to each function's docstring, once. Returns what changed.

    The docstring is normalised with inspect.cleandoc first: appending an unindented line to a
    raw indented docstring would destroy the common leading whitespace cleandoc strips, which
    would mangle every one of these docstrings in the generated reference page.  Idempotent, so
    a second configure() call (or a module imported twice) cannot double-stamp.
    """
    stamped: list[str] = []
    for fn in fns:
        doc = inspect.cleandoc(fn.__doc__ or "")
        if not doc or "PRECONDITION:" in doc:
            continue
        try:
            fn.__doc__ = f"{doc}\n\n{sentence}"
        except (AttributeError, TypeError):  # a builtin/slot wrapper: not worth failing over
            continue
        stamped.append(fn.__name__)
    return stamped


def _pick(ctx: Any, name: str) -> Any:
    """Read one dependency out of a dict-ish or attribute-ish context."""
    if isinstance(ctx, dict):
        return ctx.get(name)
    return getattr(ctx, name, None)


def configure(ctx: Any) -> dict[str, Any]:
    """Bind the server's helpers into this module. Called once, before any tool runs.

    Returns what was bound (including whether the worker script and the automation venv are
    actually present), so the server or a driver can assert the wiring instead of discovering
    a hole inside a tool call.
    """
    global KICAD_CLI, VENV_PY, REPO_ROOT, MCP_DIR
    global _run, _cli_env, _api_alive, _api

    missing = [n for n in _REQUIRED if _pick(ctx, n) is None]
    if missing:
        return {"ok": False, "error": "incomplete context", "missing": missing}

    cli = _pick(ctx, "KICAD_CLI")
    KICAD_CLI = Path(cli) if cli else None
    VENV_PY = Path(_pick(ctx, "VENV_PY"))
    REPO_ROOT = Path(_pick(ctx, "REPO_ROOT"))
    mcp_dir = _pick(ctx, "MCP_DIR")
    MCP_DIR = Path(mcp_dir) if mcp_dir else None
    _run = _pick(ctx, "run")
    _cli_env = _pick(ctx, "cli_env")
    _api_alive = _pick(ctx, "api_alive")
    _api = _pick(ctx, "api")

    worker = _worker()
    stamped = stamp_precondition(TOOLS)
    return {
        "ok": True,
        "venv_python": str(VENV_PY),
        "venv_python_exists": VENV_PY.exists(),
        "worker": str(worker),
        "worker_exists": worker.exists(),
        "tools": [fn.__name__ for fn in TOOLS],
        "precondition_stamped": stamped,
    }


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _unconfigured() -> dict[str, Any] | None:
    if _run is None or VENV_PY is None:
        return {"ok": False,
                "error": "phicad.mcp.tools_session is not configured",
                "hint": "the server must call tools_session.configure(ctx) at import time"}
    return None


def _mcp_dir() -> Path:
    if MCP_DIR is not None:
        return MCP_DIR
    root = REPO_ROOT if REPO_ROOT is not None else Path(__file__).resolve().parents[2]
    return root / "phicad" / "mcp"


def _worker() -> Path:
    """The worker next to THIS file, falling back to the configured MCP dir.

    A server pointed at another worktree (PHICAD_REPO) must run that worktree's worker, but a
    module imported from this directory should still work when nothing was configured.
    """
    local = Path(__file__).resolve().parent / "session_worker.py"
    return local if local.exists() else _mcp_dir() / "session_worker.py"


def _worker_python() -> str:
    """The AUTOMATION venv: the worker needs kipy, which the MCP venv deliberately lacks."""
    if VENV_PY is not None and Path(VENV_PY).exists():
        return str(VENV_PY)
    return shutil.which("python3") or sys.executable or "python3"


def _socket() -> str | None:
    if not _api:
        return None
    sock = _api.get("socket")
    return str(sock) if sock else None


def _call(sub: str, payload: dict[str, Any], *,
          timeout: int = WORKER_TIMEOUT_S) -> dict[str, Any]:
    """Run one worker subcommand against the managed api-server and return its JSON.

    Refuses in the same shape server.py's own `_session` does when no managed server is up,
    so an agent gets one consistent "start the server first" answer across every session tool.
    """
    if (err := _unconfigured()):
        return err
    if not VENV_PY.exists():
        return {"ok": False, "error": f"automation venv missing at {VENV_PY}",
                "hint": "phicad/bin/phicad venv"}
    if not _api_alive():
        return {"ok": False, "error": "no managed api-server running",
                "hint": "api_server_start() — the file argument is optional"}
    worker = _worker()
    if not worker.exists():
        return {"ok": False, "error": f"worker not found: {worker}",
                "hint": "phicad/mcp/session_worker.py is missing from this worktree"}
    sock = _socket()
    if not sock:
        return {"ok": False, "error": "the managed api-server has no socket path recorded",
                "hint": "api_server_stop() then api_server_start()"}

    env = _cli_env()
    env["KICAD_API_SOCKET"] = sock if sock.startswith("ipc://") else f"ipc://{sock}"
    argv = [_worker_python(), str(worker), sub, json.dumps(payload)]
    # The worker answers in JSON; clipping stdout would corrupt large listings.
    res = _run(argv, env=env, timeout=timeout, stdout_limit=None)
    if res.get("timed_out"):
        return {"ok": False, "error": res.get("error", f"timeout after {timeout}s"),
                "timed_out": True, "subcommand": sub,
                "hint": "the api-server may be busy filling zones; retry or api_server_stop()"}

    # kipy warns on stderr; the answer is the last JSON object on stdout.
    for line in reversed((res.get("stdout") or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                out = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(out, dict):
                out.setdefault("subcommand", sub)
                out["elapsed_s"] = res.get("elapsed_s")
                if not out.get("ok") and res.get("stderr"):
                    out.setdefault("stderr", (res.get("stderr") or "")[-1500:])
                return out
    return {"ok": False,
            "error": f"session worker {sub} returned no JSON (exit {res.get('exit_code')})",
            "subcommand": sub, "stdout": (res.get("stdout") or "")[-1500:],
            "stderr": (res.get("stderr") or "")[-1500:], "command": res.get("command")}


def _point(value: Any, name: str) -> tuple[list[float] | None, dict[str, Any] | None]:
    """Validate an [x_mm, y_mm] argument before it costs a subprocess."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [float(value[0]), float(value[1])], None
        except (TypeError, ValueError):
            pass
    if isinstance(value, dict) and "x_mm" in value and "y_mm" in value:
        try:
            return [float(value["x_mm"]), float(value["y_mm"])], None
        except (TypeError, ValueError):
            pass
    return None, {"ok": False, "error": f"{name} must be a [x_mm, y_mm] pair of numbers",
                  "got": value}


#: A relative move larger than this is almost certainly a units mistake (nm/mil for mm), and a
#: footprint flung 5 m off the board is far harder to notice than a refused call.
MAX_RELATIVE_MOVE_MM = 500.0

#: The item buckets session_list_items / session_delete_by_filter understand.
KINDS = ("footprint", "pad", "track", "via", "zone", "shape", "text", "dimension", "group")
#: The shapes session_add_graphic can draw.
GRAPHIC_KINDS = ("line", "rect", "circle", "arc", "poly")


def _check_kind(kind: str) -> dict[str, Any] | None:
    if (kind or "").strip().lower() not in KINDS:
        return {"ok": False, "error": f"unknown kind {kind!r}", "valid_kinds": list(KINDS)}
    return None


# --------------------------------------------------------------------------------------
# READ tools
# --------------------------------------------------------------------------------------


def session_list_items(kind: str, layer: str | None = None, net: str | None = None,
                       limit: int = 100) -> dict[str, Any]:
    """List items of one kind on the board open in the managed headless session.

    `kind` is one of footprint, pad, track, via, zone, shape, text, dimension, group.
    `layer` and `net` narrow the result (both are validated against the board: a typo comes
    back with the list of enabled layers / known net names, not an empty result).  Positions,
    widths and lengths are millimetres.  Every row carries an `id` (KIID) — that is what the
    write tools, session_get_item and session_delete_items address items by.

    `ok` means the board was readable; `matched` is the filtered count, `total_on_board` the
    unfiltered one, and `truncated` says whether `limit` cut the list short.
    """
    if (err := _check_kind(kind)):
        return err
    if limit < 1:
        return {"ok": False, "error": "limit must be >= 1"}
    return _call("list_items", {"kind": kind.strip().lower(), "layer": layer, "net": net,
                                "limit": int(limit)})


def session_get_item(item_id: str) -> dict[str, Any]:
    """Everything about ONE item, addressed by the KIID that session_list_items returned.

    Detailed by design: a footprint answers with its pad list (number, net, position) and its
    custom fields, a zone with its full outline, a text item with size/thickness/rotation.
    Fails with the id echoed back when nothing on the board has it.
    """
    if not (item_id or "").strip():
        return {"ok": False, "error": "item_id is required",
                "hint": "session_list_items(kind=...) returns the ids"}
    return _call("get_item", {"item_id": item_id.strip()})


def session_get_nets(with_items: bool = False) -> dict[str, Any]:
    """Every net on the board with its net class, sorted by name.

    `with_items=True` adds `item_count` and `items_by_kind` per net, which costs one IPC round
    trip per net — fine for a hundred nets, slow for thousands.
    """
    return _call("get_nets", {"with_items": bool(with_items)})


def session_get_stackup() -> dict[str, Any]:
    """The board's physical stackup plus its layer configuration.

    Returns `enabled_layers` (the canonical names every other session tool accepts),
    `renamed_layers` when the user gave a layer their own name, `copper_layer_count`, the
    per-layer stackup rows (type, thickness_mm, material) and the summed `board_thickness_mm`.
    """
    return _call("get_stackup", {})


def session_bounding_box(item_ids: list[str] | None = None) -> dict[str, Any]:
    """KiCad's own bounding box, in mm, for the named items or for the whole board.

    With no `item_ids` every item on the board is measured and the union is returned — which
    is how you find where a board actually sits before placing anything near it.  With ids,
    each item's own box is reported too.  Note the union covers ALL layers, including
    documentation graphics far outside the board outline.
    """
    payload: dict[str, Any] = {}
    if item_ids:
        payload["item_ids"] = [str(i) for i in item_ids]
    return _call("bounding_box", payload)


def session_hit_test(x_mm: float, y_mm: float, layer: str | None = None,
                     tolerance_mm: float = 0.0) -> dict[str, Any]:
    """Ask KiCad what is under a point — the reliable way to find the item at a location.

    Every candidate item is hit-tested by KiCad itself (no geometry is reimplemented here),
    which costs one IPC round trip per candidate: pass `layer` to cut the candidate set down,
    or expect a whole-board probe to take seconds.  `tolerance_mm` grows the probe.
    """
    return _call("hit_test", {"x_mm": float(x_mm), "y_mm": float(y_mm), "layer": layer,
                              "tolerance_mm": float(tolerance_mm)})


def session_selection(action: str = "get", item_ids: list[str] | None = None) -> dict[str, Any]:
    """Read or change the editor selection: action get | set | add | clear.

    Selection belongs to an editor FRAME, so on a headless `kicad-cli api-server` KiCad
    refuses all four actions and this returns ok=false with `headless: true` — that is a
    property of the session, not a bug.  It works when the socket points at a running PhiCad
    GUI.  Selection is never written to disk.
    """
    action = (action or "get").strip().lower()
    if action not in ("get", "set", "add", "clear"):
        return {"ok": False, "error": f"unknown action {action!r}",
                "valid": ["get", "set", "add", "clear"]}
    if action in ("set", "add") and not item_ids:
        return {"ok": False, "error": f"item_ids is required for action {action!r}"}
    payload: dict[str, Any] = {"action": action}
    if item_ids:
        payload["item_ids"] = [str(i) for i in item_ids]
    return _call("selection", payload)


def session_title_block(fields: dict[str, str] | None = None) -> dict[str, Any]:
    """Read the board's title block, or write it when `fields` is given.

    Settable keys: title, date, revision, company, and `comments` as {"1": "text", ...}.
    Unknown keys are refused with the valid list.  A write is verified by reading the title
    block back and is reported through `applied`; it is session state until session_save().
    """
    payload: dict[str, Any] = {}
    if fields is not None:
        if not isinstance(fields, dict):
            return {"ok": False, "error": "fields must be an object of "
                                          "{title,date,revision,company,comments}"}
        payload["fields"] = fields
    return _call("title_block", payload)


# --------------------------------------------------------------------------------------
# WRITE tools — footprints
# --------------------------------------------------------------------------------------


def _current_placement(ref: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(placement, error) for one footprint: position_mm / rotation_deg / side / value / locked.

    Read with a deliberate NO-OP update: `update_footprint` with only a ref refuses with
    "nothing to change" and hands back the footprint's current snapshot as `current`, which is
    exactly one IPC round trip for exactly the fields a relative write needs.  The alternative
    (list every footprint and search) serialises the whole board to find one row.  An unknown
    ref, a dead server or a closed document all fail here, with the worker's own message and
    its `known_references` list, so the error an agent sees is the real cause.
    """
    probe = _call("update_footprint", {"ref": ref})
    current = probe.get("current")
    if isinstance(current, dict) and isinstance(current.get("position_mm"), list):
        return current, None
    probe.setdefault("error", "could not read the footprint's current placement")
    probe["ok"] = False
    return None, probe


def session_update_footprint(ref: str, x_mm: float | None = None, y_mm: float | None = None,
                             rot_deg: float | None = None, side: str | None = None,
                             locked: bool | None = None, value: str | None = None,
                             reference: str | None = None,
                             fields: dict[str, str] | None = None,
                             relative: bool = False) -> dict[str, Any]:
    """Change one footprint, addressed by reference designator: any subset of its properties.

    ABSOLUTE BY DEFAULT: `x_mm`/`y_mm` are where the footprint origin ENDS UP and `rot_deg` is
    the orientation it ends up at.  With `relative=True` those three become DELTAS added to the
    footprint's current placement (read back first, so the arithmetic is KiCad's numbers, not
    yours) — "move it 2 mm left" is relative=True with x_mm=-2, "move it to x=50" is the
    default.  `relative` affects ONLY x_mm/y_mm/rot_deg; a relative move larger than 500 mm is
    refused as a units mistake.  The resolved absolute target is reported as `resolved_mm` /
    `resolved_deg` next to the `delta` you asked for.

    The rest: `side` = front|back (applied through KiCad's own FlipItems, so children mirror
    properly — a side change is a SEPARATE transaction and therefore a second undo step),
    `locked`, `value`, `reference` (the refdes itself), and `fields` for existing custom fields
    only.

    Property writes land in one commit; the whole thing is then re-read through a fresh board
    handle and `applied` reflects that read-back, with `unapplied` naming anything that did
    not stick.  Session state only: `saved` is false until session_save().
    """
    if not (ref or "").strip():
        return {"ok": False, "error": "ref is required (a footprint reference like 'U1')"}
    if side is not None and side.strip().lower() not in ("front", "back", "top", "bottom"):
        return {"ok": False, "error": "side must be front|back", "got": side}
    if fields is not None and not isinstance(fields, dict):
        return {"ok": False, "error": "fields must be an object of {field_name: value}"}
    payload: dict[str, Any] = {"ref": ref.strip()}
    for key, val in (("x_mm", x_mm), ("y_mm", y_mm), ("rot_deg", rot_deg), ("side", side),
                     ("locked", locked), ("value", value), ("reference", reference),
                     ("fields", fields)):
        if val is not None:
            payload[key] = val
    if len(payload) == 1:
        return {"ok": False,
                "error": "nothing to change: pass at least one of x_mm, y_mm, rot_deg, side, "
                         "locked, value, reference, fields"}

    delta: dict[str, float] = {}
    if relative:
        if x_mm is None and y_mm is None and rot_deg is None:
            return {"ok": False,
                    "error": "relative=True needs at least one of x_mm, y_mm, rot_deg "
                             "(it only applies to placement)",
                    "hint": "drop relative=True for a side/locked/value/reference/fields write"}
        for name, val in (("x_mm", x_mm), ("y_mm", y_mm)):
            if val is not None and abs(float(val)) > MAX_RELATIVE_MOVE_MM:
                return {"ok": False,
                        "error": f"refusing a relative move larger than "
                                 f"{MAX_RELATIVE_MOVE_MM:g} mm ({name}={val})",
                        "hint": "drop relative=True to place at coordinates that large"}
        current, err = _current_placement(ref.strip())
        if err:
            return err
        cx, cy = float(current["position_mm"][0]), float(current["position_mm"][1])
        crot = float(current.get("rotation_deg") or 0.0)
        if x_mm is not None:
            delta["x_mm"] = float(x_mm)
            cx += float(x_mm)
            payload["x_mm"] = cx
        if y_mm is not None:
            delta["y_mm"] = float(y_mm)
            cy += float(y_mm)
            payload["y_mm"] = cy
        if rot_deg is not None:
            delta["rot_deg"] = float(rot_deg)
            crot = (crot + float(rot_deg)) % 360.0
            payload["rot_deg"] = crot

    res = _call("update_footprint", payload)
    res["relative"] = bool(relative)
    if relative:
        res["delta"] = delta
        # The FULL resolved target, including the axis the caller left alone: [x, None] would
        # read as "y is unknown" when it is simply unchanged.
        res["resolved_mm"] = [cx, cy]
        res["resolved_deg"] = crot
    return res


def session_batch_move(moves: list[dict[str, Any]]) -> dict[str, Any]:
    """Move (and optionally rotate) many footprints to absolute positions in ONE undo step.

    `moves` is [{"ref": "R1", "x_mm": 10.0, "y_mm": 20.0, "rot_deg": 90.0}, ...] — x_mm, y_mm
    and rot_deg are each optional per row, so a row can rotate in place.  The whole batch is
    one commit: it is pushed only if KiCad accepted it, dropped otherwise, so the board is
    never left half-placed.  Every row is verified against a fresh read and reported with its
    own `applied`; `unapplied_refs` names the failures.
    """
    if not isinstance(moves, list) or not moves:
        return {"ok": False,
                "error": "moves must be a non-empty list of {ref, x_mm, y_mm, rot_deg}"}
    for row in moves:
        if not isinstance(row, dict) or not (row.get("ref") or "").strip():
            return {"ok": False, "error": "every move needs a ref", "got": row}
        if row.get("x_mm") is None and row.get("y_mm") is None and row.get("rot_deg") is None:
            return {"ok": False,
                    "error": f"move for {row.get('ref')} changes nothing "
                             "(needs x_mm, y_mm or rot_deg)"}
    return _call("batch_move", {"moves": moves})


# --------------------------------------------------------------------------------------
# WRITE tools — creation
# --------------------------------------------------------------------------------------


def session_add_track(start_mm: list[float], end_mm: list[float], layer: str,
                      width_mm: float, net: str | None = None) -> dict[str, Any]:
    """Add one straight copper track segment to the open board.

    `layer` must be an enabled copper layer (session_get_stackup lists them; a bad name comes
    back with the valid list).  `net` is an EXISTING net name — a track with no net is legal
    and routes nothing, so pass the net you mean.  Zero-length segments are refused.

    Returns the KIID KiCad assigned in `created_ids`, plus the item read back off a fresh
    board in `created`.  Session state: `saved` is false until session_save().
    """
    start, err = _point(start_mm, "start_mm")
    if err:
        return err
    end, err = _point(end_mm, "end_mm")
    if err:
        return err
    if not (layer or "").strip():
        return {"ok": False, "error": "layer is required"}
    if width_mm is None or float(width_mm) <= 0:
        return {"ok": False, "error": "width_mm must be a positive number"}
    return _call("add_track", {"start_mm": start, "end_mm": end, "layer": layer,
                               "width_mm": float(width_mm), "net": net})


def session_add_via(at_mm: list[float], drill_mm: float, size_mm: float,
                    net: str | None = None, start_layer: str = "F.Cu",
                    end_layer: str = "B.Cu") -> dict[str, Any]:
    """Add one via at a position, with a drill and a pad diameter in mm.

    `size_mm` is the copper pad diameter and must exceed `drill_mm`.  `start_layer`/`end_layer`
    are the copper span the barrel drills (the default F.Cu..B.Cu is a through via); both must
    be enabled on the board.  `net` is an existing net name.

    Returns the new KIID plus the via read back from a fresh board (drill, diameter, span).
    Writes to the SESSION only (`outputs: []`, `mutated: []`, `saved: false`); session_save()
    is what writes the board file.
    """
    at, err = _point(at_mm, "at_mm")
    if err:
        return err
    if drill_mm is None or float(drill_mm) <= 0:
        return {"ok": False, "error": "drill_mm must be a positive number"}
    if size_mm is None or float(size_mm) <= 0:
        return {"ok": False, "error": "size_mm must be a positive number"}
    if float(size_mm) <= float(drill_mm):
        return {"ok": False,
                "error": f"size_mm ({size_mm}) must be larger than drill_mm ({drill_mm})"}
    return _call("add_via", {"at_mm": at, "drill_mm": float(drill_mm),
                             "size_mm": float(size_mm), "net": net,
                             "start_layer": start_layer, "end_layer": end_layer})


def session_add_zone(polygon_mm: list[list[float]], layer: str, net: str | None = None,
                     name: str | None = None, priority: int = 0) -> dict[str, Any]:
    """Add a copper zone from a closed polygon of [x_mm, y_mm] points (>= 3).

    The polygon is closed for you.  `net` is the net the copper pours for — a zone with no net
    is a bare copper area that connects nothing.  `priority` decides which zone wins where two
    overlap (higher wins).

    The zone is created UNFILLED: it contributes no connectivity until session_refill_zones().
    Returns the new KIID and the zone read back from a fresh board.
    """
    if not isinstance(polygon_mm, list) or len(polygon_mm) < 3:
        return {"ok": False,
                "error": "polygon_mm must be a list of at least 3 [x_mm, y_mm] points"}
    for pt in polygon_mm:
        _, err = _point(pt, "polygon_mm point")
        if err:
            return err
    if not (layer or "").strip():
        return {"ok": False, "error": "layer is required"}
    if priority < 0:
        return {"ok": False, "error": "priority must be >= 0"}
    return _call("add_zone", {"polygon_mm": polygon_mm, "layer": layer, "net": net,
                              "name": name, "priority": int(priority)})


def session_add_text(text: str, at_mm: list[float], layer: str, size_mm: float = 1.0,
                     thickness_mm: float | None = None,
                     rotation_deg: float = 0.0) -> dict[str, Any]:
    """Add free text to the open board on any enabled layer.

    `size_mm` is the glyph height/width; `thickness_mm` defaults to 15% of it, which is
    KiCad's own stroke-font ratio.  `rotation_deg` is counter-clockwise.  Text on a copper
    layer is copper and will be checked by DRC like any other copper.

    Returns the new KIID plus the text read back from a fresh board.  Writes to the SESSION
    only (`outputs: []`, `mutated: []`, `saved: false`); session_save() writes the board file.
    """
    if not (text or ""):
        return {"ok": False, "error": "text is required and must be non-empty"}
    at, err = _point(at_mm, "at_mm")
    if err:
        return err
    if not (layer or "").strip():
        return {"ok": False, "error": "layer is required"}
    if size_mm is None or float(size_mm) <= 0:
        return {"ok": False, "error": "size_mm must be a positive number"}
    if thickness_mm is not None and float(thickness_mm) <= 0:
        return {"ok": False, "error": "thickness_mm must be a positive number"}
    payload: dict[str, Any] = {"text": text, "at_mm": at, "layer": layer,
                               "size_mm": float(size_mm),
                               "rotation_deg": float(rotation_deg)}
    if thickness_mm is not None:
        payload["thickness_mm"] = float(thickness_mm)
    return _call("add_text", payload)


def session_add_graphic(kind: str, layer: str, width_mm: float,
                        points_mm: list[list[float]] | None = None,
                        center_mm: list[float] | None = None,
                        radius_mm: float | None = None,
                        start_mm: list[float] | None = None,
                        end_mm: list[float] | None = None) -> dict[str, Any]:
    """Add a graphic shape: kind = line | rect | circle | arc | poly.

    Which arguments matter depends on `kind`:
        line    start_mm + end_mm
        rect    start_mm + end_mm (opposite corners; normalised for you)
        circle  center_mm + radius_mm
        arc     start_mm + end_mm + center_mm (the short way round; KiCad stores start/mid/end)
        poly    points_mm, at least 3 points, closed for you
    `width_mm` is the stroke width.  On Edge.Cuts these shapes ARE the board outline; on a
    copper layer they are copper.

    Returns the new KIID plus the shape read back from a fresh board.  Writes to the SESSION
    only (`outputs: []`, `mutated: []`, `saved: false`); session_save() writes the board file.
    """
    kind = (kind or "").strip().lower()
    if kind not in GRAPHIC_KINDS:
        return {"ok": False, "error": f"unknown graphic kind {kind!r}",
                "valid_kinds": list(GRAPHIC_KINDS)}
    if not (layer or "").strip():
        return {"ok": False, "error": "layer is required"}
    if width_mm is None or float(width_mm) <= 0:
        return {"ok": False, "error": "width_mm must be a positive number"}

    payload: dict[str, Any] = {"kind": kind, "layer": layer, "width_mm": float(width_mm)}
    need: tuple[str, ...] = {
        "line": ("start_mm", "end_mm"),
        "rect": ("start_mm", "end_mm"),
        "circle": ("center_mm", "radius_mm"),
        "arc": ("start_mm", "end_mm", "center_mm"),
        "poly": ("points_mm",),
    }[kind]
    given = {"start_mm": start_mm, "end_mm": end_mm, "center_mm": center_mm,
             "radius_mm": radius_mm, "points_mm": points_mm}
    for arg in need:
        if given[arg] is None:
            return {"ok": False, "error": f"{kind} needs {', '.join(need)}",
                    "missing": arg}
    for arg in ("start_mm", "end_mm", "center_mm"):
        if arg in need:
            pt, err = _point(given[arg], arg)
            if err:
                return err
            payload[arg] = pt
    if "radius_mm" in need:
        if float(radius_mm) <= 0:
            return {"ok": False, "error": "radius_mm must be a positive number"}
        payload["radius_mm"] = float(radius_mm)
    if "points_mm" in need:
        if not isinstance(points_mm, list) or len(points_mm) < 3:
            return {"ok": False, "error": "poly needs at least 3 [x_mm, y_mm] points"}
        for pt in points_mm:
            _, err = _point(pt, "points_mm point")
            if err:
                return err
        payload["points_mm"] = points_mm
    return _call("add_graphic", payload)


# --------------------------------------------------------------------------------------
# WRITE tools — deletion, nets, board settings
# --------------------------------------------------------------------------------------


def session_delete_items(item_ids: list[str]) -> dict[str, Any]:
    """Delete the given items from the open board by KIID, in one undo step.

    Ids that do not exist are reported in `missing_ids` rather than silently dropped, and the
    delete is verified by proving the ids are gone from a fresh read (`still_present` names any
    item KiCad refused to remove).  Session state until session_save().
    """
    if not isinstance(item_ids, list) or not item_ids:
        return {"ok": False, "error": "item_ids is required (a list of KIID strings)",
                "hint": "session_list_items(kind=...) returns the ids"}
    return _call("delete_items", {"item_ids": [str(i) for i in item_ids]})


def session_delete_by_filter(kind: str, layer: str | None = None, net: str | None = None,
                             dry_run: bool = True) -> dict[str, Any]:
    """Delete every item of a kind, optionally narrowed to a layer and/or net.

    DRY RUN BY DEFAULT: with `dry_run=True` nothing is touched and the answer is
    `would_delete` plus a sample of up to 50 items, because "delete every track on F.Cu" is
    one keystroke from erasing a board's routing.  Pass `dry_run=False` to actually apply it;
    the deletion is one commit and is verified against a fresh read.
    """
    if (err := _check_kind(kind)):
        return err
    return _call("delete_by_filter", {"kind": kind.strip().lower(), "layer": layer,
                                      "net": net, "dry_run": bool(dry_run)})


def session_set_net(item_ids: list[str], net_name: str) -> dict[str, Any]:
    """Assign an existing net to the given items (tracks, vias, zones, pads, copper shapes).

    `net_name` must already exist on the board — this does not create nets (an unknown name
    comes back with the known list).  Pass "" to clear an item's net.  Item kinds that cannot
    carry a net are reported in `skipped` instead of failing the call.  One commit, verified
    by re-reading each item's net from a fresh board.
    """
    if not isinstance(item_ids, list) or not item_ids:
        return {"ok": False, "error": "item_ids is required (a list of KIID strings)"}
    if net_name is None:
        return {"ok": False, "error": "net_name is required (use \"\" to clear the net)"}
    return _call("set_net", {"item_ids": [str(i) for i in item_ids], "net_name": net_name})


def session_set_enabled_layers(layer_names: list[str]) -> dict[str, Any]:
    """Set the board's enabled layer set. DESTRUCTIVE and NOT UNDOABLE through this API.

    Pass the complete list of layers that should be enabled, canonical names ("F.Cu", "In1.Cu",
    "Edge.Cuts", ...).  The copper layer count is derived from the copper names in the list and
    must be even and >= 2.  ANY CONTENT ON A LAYER YOU LEAVE OUT IS DELETED — KiCad also keeps
    a few layers enabled regardless, which the result reports as `extra_enabled`.

    Verified by reading the enabled set back; `missing_requested` names anything KiCad refused.
    Writes to the SESSION only (`outputs: []`, `mutated: []`, `saved: false`) — but the deleted
    content is gone from the session immediately; session_save() writes the board file.
    """
    if not isinstance(layer_names, list) or not layer_names:
        return {"ok": False, "error": "layer_names must be a non-empty list of layer names",
                "hint": "session_get_stackup() lists the current enabled layers"}
    return _call("set_enabled_layers", {"layer_names": [str(n) for n in layer_names]})


def session_set_origin(x_mm: float, y_mm: float, which: str = "grid") -> dict[str, Any]:
    """Move the grid origin (`which='grid'`) or the drill/place-file origin (`which='drill'`).

    The drill origin is what exported drill and placement files are measured from, so changing
    it changes every subsequent fabrication export.  Verified by reading the origin back.
    Writes to the SESSION only (`outputs: []`, `mutated: []`, `saved: false`); session_save()
    writes the board file.
    """
    which = (which or "grid").strip().lower()
    if which not in ("grid", "drill", "place"):
        return {"ok": False, "error": "which must be grid|drill", "valid": ["grid", "drill"]}
    return _call("set_origin", {"x_mm": float(x_mm), "y_mm": float(y_mm), "which": which})


def session_refill_zones() -> dict[str, Any]:
    """Refill every zone on the board and report which ones actually ended up filled.

    Blocking and genuinely slow on a dense board (minutes), which is why this has its own
    long timeout.  A zone that stays unfilled usually has no net, no island big enough to
    keep, or sits outside the board outline — the per-zone `filled` flags are the evidence.
    The new fills are written to the SESSION only (`outputs: []`, `mutated: []`,
    `saved: false`); session_save() writes them into the board file.
    """
    return _call("refill_zones", {}, timeout=REFILL_TIMEOUT_S)


def session_run_action(action_name: str) -> dict[str, Any]:
    """Run a named KiCad TOOL_ACTION in the session (escape hatch for anything unexposed).

    UNSTABLE by KiCad's own warning: action names are not API and change between versions.
    A headless `kicad-cli api-server` has no editor frame, so it refuses RunAction outright and
    this returns ok=false with `headless: true`; it works against a running PhiCad GUI.  Even
    then, RAS_OK only means the action was dispatched — re-read the board to see if it did
    anything.
    """
    if not (action_name or "").strip():
        return {"ok": False, "error": "action_name is required",
                "hint": "e.g. 'pcbnew.Control.zoneFillAll' — names are build-specific"}
    return _call("run_action", {"action_name": action_name.strip()})


def session_revert() -> dict[str, Any]:
    """Discard every unsaved session change by reloading the board from its file on disk.

    NOT RECOVERABLE: unsaved edits are gone.  A headless api-server refuses RevertDocument
    (it needs an editor frame), so this returns ok=false with `headless: true` and the
    workaround it names — session_close() then session_open() on the same path reloads the
    file, which is what revert would have done.
    """
    return _call("revert", {})


#: What the server registers. Kept as data so registration cannot drift from the module.
TOOLS = (session_list_items, session_get_item, session_get_nets, session_get_stackup,
         session_bounding_box, session_hit_test, session_selection, session_title_block,
         session_update_footprint, session_batch_move, session_add_track, session_add_via,
         session_add_zone, session_add_text, session_add_graphic, session_delete_items,
         session_delete_by_filter, session_set_net, session_set_enabled_layers,
         session_set_origin, session_refill_zones, session_run_action, session_revert)
