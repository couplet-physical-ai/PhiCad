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

"""Whitelisted WRITE operations on the open board, for the AI View chat pane.

Design, and why it is shaped this way
-------------------------------------
The chat pane is a text box driven by a language model, and a text box that can silently
mutate a PCB is a bad idea.  So the model never writes.  Instead:

    the USER types an explicit command  ->  we validate it  ->  we apply it  ->  we read it
    back from a FRESH board object  ->  we report what actually changed

The model's role is only to *recommend* the exact command, which the user can read before
running.  Human-in-the-loop is therefore structural, not a policy the model could ignore,
and there is no output-parsing step that could misfire.

Nothing here saves the board.  Edits land in the editor as undoable changes, so a wrong
command is one Cmd-Z away; `/save` is separate and explicit.

Three traps this module exists to encode
----------------------------------------
1. **kipy getters return COPIES.**  `fp.position.x = ...` mutates a temporary and is a silent
   no-op.  Every write here builds a new value and assigns it to the attribute.
2. **A successful update call does not mean the board changed.**  `ISC_OK` is returned for
   discarded edits too, so every operation re-reads the item from a fresh `get_board()` and
   compares; a mismatch is reported as a failure rather than as success.
3. **Reference designators are not unique across an unopened project.**  We resolve against
   the *open* board only, and refuse ambiguous or unknown refs instead of guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Tolerance when comparing a value we wrote against the value we read back.  Board internal
# units are nanometres, so anything at or below this is representation noise, not a discard.
VERIFY_EPS_MM = 1e-4
VERIFY_EPS_DEG = 1e-3

# Refuse moves that are almost certainly a typo (mm vs internal units, or a stray zero).
MAX_MOVE_MM = 500.0


class EditError(RuntimeError):
    """The command was rejected, or was applied but did not take effect."""


@dataclass
class EditResult:
    """What a single command did, in words the pane can print verbatim."""

    summary: str
    detail: str = ""

    def __str__(self) -> str:
        return self.summary if not self.detail else f"{self.summary}\n{self.detail}"


# ---------------------------------------------------------------------------------------
# resolution helpers
# ---------------------------------------------------------------------------------------
def _footprints(board):
    """All footprints on a board.

    Uses get_footprints() rather than get_items(FootprintInstance): get_items wants an
    iterable of types, and more importantly this fork sends ReferencePoint children that
    kipy cannot unwrap, so board_context.install_fork_compat() must have run first.  It is
    idempotent, so calling it here keeps board_edits usable standalone.
    """
    import board_context

    board_context.install_fork_compat()
    return list(board.get_footprints())


def _ref_of(fp) -> str:
    try:
        return fp.reference_field.text.value or ""
    except Exception:
        return ""


def find_footprint(board, ref: str):
    """Resolve a reference designator on the OPEN board, or raise a helpful EditError."""
    want = ref.strip().upper()
    hits = [fp for fp in _footprints(board) if _ref_of(fp).upper() == want]

    if not hits:
        known = sorted({_ref_of(fp) for fp in _footprints(board) if _ref_of(fp)})
        raise EditError(
            f"no footprint {ref!r} on the open board.\n"
            f"known references: {', '.join(known) if known else '(none)'}"
        )
    if len(hits) > 1:
        raise EditError(
            f"{len(hits)} footprints share the reference {ref!r}; refusing to guess which."
        )
    return hits[0]


def _fresh(kicad, ref: str):
    """Re-read one footprint through a NEW board handle, for verification."""
    return find_footprint(kicad.get_board(), ref)


def _pos_mm(fp) -> tuple[float, float]:
    p = fp.position
    return (p.x / 1e6, p.y / 1e6)


def _orient_deg(fp) -> float:
    return float(fp.orientation.degrees)


# ---------------------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------------------
def move_footprint(kicad, board, ref: str, dx_mm: float, dy_mm: float,
                   absolute: bool = False) -> EditResult:
    """Move a footprint by (dx, dy) mm, or to an absolute (x, y) when `absolute`."""
    for v, name in ((dx_mm, "x"), (dy_mm, "y")):
        if not math.isfinite(v):
            raise EditError(f"{name} is not a finite number")
        if not absolute and abs(v) > MAX_MOVE_MM:
            raise EditError(
                f"refusing a {v:g} mm move in {name}: larger than MAX_MOVE_MM"
                f" ({MAX_MOVE_MM:g} mm). If that was really intended, do it in the editor."
            )

    fp = find_footprint(board, ref)
    ox, oy = _pos_mm(fp)
    nx, ny = (dx_mm, dy_mm) if absolute else (ox + dx_mm, oy + dy_mm)

    from kipy.geometry import Vector2

    # Assignment, NOT fp.position.x = ... -- the getter hands back a copy.
    fp.position = Vector2.from_xy_mm(nx, ny)
    board.update_items(fp)

    gx, gy = _pos_mm(_fresh(kicad, ref))
    if abs(gx - nx) > VERIFY_EPS_MM or abs(gy - ny) > VERIFY_EPS_MM:
        raise EditError(
            f"{ref}: wrote ({nx:.4f}, {ny:.4f}) mm but the board reads back "
            f"({gx:.4f}, {gy:.4f}) mm -- the edit was discarded, not applied."
        )
    return EditResult(
        f"moved {ref} to ({gx:.3f}, {gy:.3f}) mm",
        f"was ({ox:.3f}, {oy:.3f}) mm; delta ({gx - ox:+.3f}, {gy - oy:+.3f}) mm. "
        "Not saved -- Cmd-Z in the editor undoes it, /save writes it to disk.",
    )


def rotate_footprint(kicad, board, ref: str, degrees: float,
                     relative: bool = False) -> EditResult:
    """Set a footprint's orientation, or add to it when `relative`."""
    if not math.isfinite(degrees):
        raise EditError("angle is not a finite number")

    fp = find_footprint(board, ref)
    old = _orient_deg(fp)
    new = (old + degrees) if relative else degrees

    from kipy.geometry import Angle

    fp.orientation = Angle.from_degrees(new)
    board.update_items(fp)

    got = _orient_deg(_fresh(kicad, ref))
    # Orientation is stored normalised, so compare on the circle rather than numerically.
    if min(abs(got - new) % 360.0, 360.0 - (abs(got - new) % 360.0)) > VERIFY_EPS_DEG:
        raise EditError(
            f"{ref}: wrote {new:g} deg but the board reads back {got:g} deg "
            "-- the edit was discarded, not applied."
        )
    return EditResult(
        f"rotated {ref} to {got:g} deg",
        f"was {old:g} deg. Not saved -- Cmd-Z undoes it, /save writes it to disk.",
    )


def set_value(kicad, board, ref: str, text: str) -> EditResult:
    """Set a footprint's Value field text."""
    if not text.strip():
        raise EditError("refusing to set an empty value; pass the text you want")

    fp = find_footprint(board, ref)
    old = fp.value_field.text.value

    # The Field -> BoardText -> value chain is all copies on the way out, so mutate the
    # field object we hold and push the whole footprint back.
    fp.value_field.text.value = text
    board.update_items(fp)

    got = _fresh(kicad, ref).value_field.text.value
    if got != text:
        raise EditError(
            f"{ref}: wrote value {text!r} but the board reads back {got!r} "
            "-- the edit was discarded, not applied."
        )
    return EditResult(
        f"set {ref} value to {text!r}",
        f"was {old!r}. Not saved -- Cmd-Z undoes it, /save writes it to disk.",
    )


def save_board(kicad, board) -> EditResult:
    """Save the open board to disk.

    The import path is the trap here, and there are two layers to it.  SaveDocument is a
    PROJECT command, not an editor one -- and it does not live under `kipy.proto.project`
    either (there is no such package).  The real path is
    `kipy.proto.common.commands.project_commands_pb2`, the same place CloseDocument lives.

    The reply is Empty, not a SaveDocumentResponse; asking for the latter fails.
    """
    from google.protobuf.empty_pb2 import Empty
    from kipy.proto.common.commands.project_commands_pb2 import SaveDocument

    doc = board.document
    req = SaveDocument()
    req.document.type = doc.type
    req.document.board_filename = doc.board_filename
    kicad._client.send(req, Empty)
    return EditResult(
        "saved the board to disk",
        "The file is rewritten in PhiCad's 10.99 format; stable KiCad 10.0.x will refuse to "
        "open it.",
    )


# ---------------------------------------------------------------------------------------
# command parsing
# ---------------------------------------------------------------------------------------
# One table, three consumers: /help prints it, the entry box completes from it, and HELP
# is generated from it.  See view_commands.COMMANDS for the shape.
COMMANDS = (
    ("/move", "<REF> <dx> <dy>", "move by dx,dy mm, e.g. /move D1 0 -0.5"),
    ("/place", "<REF> <x> <y>", "move to absolute mm, e.g. /place D1 151.5 106.6"),
    ("/rotate", "<REF> <deg>", "set orientation, e.g. /rotate J5 90"),
    ("/turn", "<REF> <deg>", "add to orientation, e.g. /turn J5 -90"),
    ("/value", "<REF> <text>", "set the Value field, e.g. /value R3 2k2"),
    ("/save", "", "write the board to disk"),
    ("/help", "", "every command, including /view, /mode and /design"),
)


def _help() -> str:
    body = "\n".join(f"  {name} {args}".ljust(38) + summary
                     for name, args, summary in COMMANDS)
    return (
        "Write commands (these change the open board; nothing is saved until you run "
        "/save):\n\n" + body + "\n\n"
        "Every command reads the change back from the board and tells you if it did not "
        "take. Cmd-Z in the PCB editor undoes anything except /save.")


HELP = _help()


def is_command(line: str) -> bool:
    return line.lstrip().startswith("/")


def run_command(kicad, board, line: str) -> EditResult:
    """Parse and execute one slash command.  Raises EditError with usage on bad input."""
    parts = line.strip().split()
    if not parts:
        raise EditError("empty command")

    cmd, args = parts[0].lower(), parts[1:]

    def nums(n: int, what: str) -> list[float]:
        if len(args) != n + 1:
            raise EditError(f"usage: {what}")
        try:
            return [float(a) for a in args[1:]]
        except ValueError as exc:
            raise EditError(f"{exc}\nusage: {what}") from None

    if cmd == "/help":
        return EditResult(HELP)
    if cmd == "/save":
        if args:
            raise EditError("usage: /save   (takes no arguments)")
        return save_board(kicad, board)
    if cmd == "/move":
        dx, dy = nums(2, "/move <REF> <dx> <dy>")
        return move_footprint(kicad, board, args[0], dx, dy)
    if cmd == "/place":
        x, y = nums(2, "/place <REF> <x> <y>")
        return move_footprint(kicad, board, args[0], x, y, absolute=True)
    if cmd == "/rotate":
        (deg,) = nums(1, "/rotate <REF> <deg>")
        return rotate_footprint(kicad, board, args[0], deg)
    if cmd == "/turn":
        (deg,) = nums(1, "/turn <REF> <deg>")
        return rotate_footprint(kicad, board, args[0], deg, relative=True)
    if cmd == "/value":
        if len(args) < 2:
            raise EditError("usage: /value <REF> <text>")
        return set_value(kicad, board, args[0], " ".join(args[1:]))

    raise EditError(f"unknown command {cmd!r}. /help lists what is available.")
