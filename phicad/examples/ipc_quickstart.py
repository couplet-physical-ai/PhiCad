#!/usr/bin/env python3
"""IPC quickstart: drive PhiCad headlessly via kipy.

Run it:
    phicad venv                                   # once
    cp -r demos/microwave /tmp/ipc-demo           # never edit the demo in-tree
    phicad api-server /tmp/ipc-demo/microwave.kicad_pcb &
    phicad py phicad/examples/ipc_quickstart.py
    pkill -f 'kicad-cli api-server'               # `kill %1` only works in the
                                                  # shell that started it

`phicad py` presets KICAD_API_SOCKET so kipy.KiCad() connects to the
`phicad api-server` instance. The server answers AS_NOT_READY while the
document is still loading, and the socket does not exist at all for the
first moment — connect_with_retry() below handles both.
Verified against build/mac-dev (10.99.0).
"""
import time

from kipy import KiCad
from kipy.board_types import BoardText
from kipy.geometry import Vector2
from kipy.proto.board import board_types_pb2


def connect_with_retry(attempts: int = 10, delay: float = 0.5) -> KiCad:
    """The server needs a beat to create the socket and load the document."""
    for i in range(attempts):
        try:
            kicad = KiCad()  # connects via KICAD_API_SOCKET
            kicad.ping()     # raises while the server reports AS_NOT_READY
            return kicad
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(delay)
    raise RuntimeError("unreachable")


def main() -> None:
    kicad = connect_with_retry()
    print("connected:", kicad.get_version())

    board = kicad.get_board()
    print("board:", board.name)
    print("  footprints:", len(board.get_footprints()))
    print("  nets      :", len(board.get_nets()))
    print("  tracks    :", len(board.get_tracks()))

    # A transactional edit. Rules that matter:
    #  - always set a real layer: layer 0 (BL_UNKNOWN) items are accepted by
    #    create_items but silently dropped when the board is saved;
    #  - position must be ASSIGNED a Vector2: `text.position.x_nm = ...`
    #    mutates a temporary copy and the write is silently lost;
    #  - push_commit makes the change undoable/visible; save() writes the file
    #    (in this fork's format - stable KiCad 10.0 will not reopen it).
    commit = board.begin_commit()
    text = BoardText()
    text.value = "hello from kipy"
    text.layer = board_types_pb2.BoardLayer.Value("BL_F_SilkS")
    text.position = Vector2.from_xy_mm(50, 50)
    created = board.create_items(text)
    print("created:", [type(item).__name__ for item in created])
    board.push_commit(commit, "ipc_quickstart: add text")
    board.save()
    print("saved.")


if __name__ == "__main__":
    main()
