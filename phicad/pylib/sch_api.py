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
"""Call this fork's schematic IPC commands from Python, without forking kipy.

PyPI kipy is generated from upstream protos and knows nothing about
`kiapi.schematic.commands.GetSchematicNetlist` / `GetSchematicHierarchy`.
Rather than maintaining a regenerated kipy, this module loads a
**descriptor-set file** compiled from `api/proto` (`phicad venv` writes it to
`<venv>/phicad-api.desc`) into a **private** descriptor pool and builds the
message classes dynamically.  Two properties make that sound:

  * `Any.Pack`/`Unpack` match on the protobuf *full name* (the `kiapi.…`
    package in the .proto files), which dynamic classes carry exactly like
    generated ones — so the messages travel through kipy's own transport
    (`KiCadClient.send`) unchanged.
  * The pool is private, never `descriptor_pool.Default()`: kipy's generated
    modules already registered the shared `kiapi.common.types` files in the
    default pool, and registering them twice is an ImportError at a distance.

Everything returned is plain Python (dicts, strings, ints) so callers never
touch a cross-pool message object by accident.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

_DESC_ENV = "PHICAD_API_DESC"

# kiapi.common.types.DocumentType — stable protocol constants, mirrored here
# because the private pool is loaded lazily and callers should not need it.
DOCTYPE_SCHEMATIC = 1


class SchApiUnavailable(RuntimeError):
    """The descriptor set is missing or unreadable; the caller should degrade."""


def _desc_candidates() -> List[Path]:
    explicit = os.environ.get(_DESC_ENV, "").strip()
    out = [Path(explicit)] if explicit else []
    # This file lives at <repo>/phicad/pylib/sch_api.py; the venv is a sibling
    # of the build trees at <repo>/build/venv (see bin/phicad cmd_venv).
    repo = Path(__file__).resolve().parent.parent.parent
    out.append(repo / "build" / "venv" / "phicad-api.desc")
    return out


class _Pool:
    """Lazily loaded private pool + message classes; one per process."""

    _instance: Optional["_Pool"] = None

    def __init__(self, desc_path: Path):
        fds = descriptor_pb2.FileDescriptorSet.FromString(desc_path.read_bytes())
        self.pool = descriptor_pool.DescriptorPool()
        for file_proto in fds.file:
            self.pool.Add(file_proto)
        self.desc_path = desc_path

    @classmethod
    def get(cls) -> "_Pool":
        if cls._instance is None:
            for candidate in _desc_candidates():
                if candidate.is_file():
                    cls._instance = cls(candidate)
                    break
            else:
                raise SchApiUnavailable(
                    "fork API descriptor set not found (run `phicad venv`, or set "
                    f"${_DESC_ENV}); looked at: "
                    + ", ".join(str(c) for c in _desc_candidates()))
        return cls._instance

    def message_class(self, full_name: str):
        return message_factory.GetMessageClass(
            self.pool.FindMessageTypeByName(full_name))

    def message(self, full_name: str):
        return self.message_class(full_name)()


def message_class(full_name: str):
    """The dynamic message CLASS for any type in the fork descriptor set.

    Public because it serves consumers beyond the schematic commands: kipy 0.8
    dropped generated classes the fork still sends (kiapi.board.types.
    ReferencePoint), and board_context's compat shim rebuilds them from here.
    Raises SchApiUnavailable / KeyError when the set or type is absent.
    """
    return _Pool.get().message_class(full_name)


def available() -> bool:
    try:
        _Pool.get()
        return True
    except SchApiUnavailable:
        return False


def _send(kicad, command, response_full_name: str):
    """One fork command through kipy's transport; returns the dynamic response."""
    response = _Pool.get().message(response_full_name)
    client = getattr(kicad, "_client", None)
    if client is None or not hasattr(client, "send"):
        raise SchApiUnavailable("kipy exposes no client transport to reuse")
    return client.send(command, type(response))


def _document(pool: _Pool, kicad, project_name: str, project_path: str):
    """The DocumentSpecifier the handlers will accept.

    With no explicit name/path, ask the editor which schematic it has open
    (kipy's own GetOpenDocuments) and mirror that specifier byte-for-byte into
    the private pool — handlers validate the request document against the open
    one, so echoing is the only always-correct construction.
    """
    doc = pool.message("kiapi.common.types.DocumentSpecifier")
    if not project_name and not project_path:
        from kipy.proto.common.commands import editor_commands_pb2
        req = editor_commands_pb2.GetOpenDocuments()
        req.type = DOCTYPE_SCHEMATIC
        resp = kicad._client.send(req, editor_commands_pb2.GetOpenDocumentsResponse)
        if not resp.documents:
            raise SchApiUnavailable("no schematic document is open in the editor")
        doc.ParseFromString(resp.documents[0].SerializeToString())
        return doc
    doc.type = DOCTYPE_SCHEMATIC
    doc.project.name = project_name or ""
    doc.project.path = project_path or ""
    return doc


def netlist(kicad, project_name: str = "", project_path: str = "") -> List[Dict[str, Any]]:
    """Live nets from the running schematic editor.

    Returns ``[{"name": str, "members": int, "sheets": int}]`` sorted by name —
    member counts are connectable items (pins, labels, wires) summed across
    sheet instances.  Raises SchApiUnavailable when the plumbing is absent;
    transport/handler errors propagate as kipy's own exceptions so the caller
    can distinguish "cannot ask" from "asked and it failed".
    """
    pool = _Pool.get()
    cmd = pool.message("kiapi.schematic.commands.GetSchematicNetlist")
    cmd.document.CopyFrom(_document(pool, kicad, project_name, project_path))
    resp = _send(kicad, cmd, "kiapi.schematic.commands.SchematicNetlistResponse")
    nets = []
    for net in resp.nets:
        nets.append({
            "name": net.name,
            "members": sum(len(sheet.items) for sheet in net.sheets),
            "sheets": len(net.sheets),
        })
    nets.sort(key=lambda n: n["name"])
    return nets


def hierarchy(kicad, project_name: str = "", project_path: str = "") -> List[Dict[str, Any]]:
    """The sheet tree, flattened depth-first with a depth marker.

    ``[{"name": str, "filename": str, "page": str, "depth": int}]`` —
    SheetInstance per schematic_types.proto (name/filename/page_number/children).
    """
    pool = _Pool.get()
    cmd = pool.message("kiapi.schematic.commands.GetSchematicHierarchy")
    cmd.document.CopyFrom(_document(pool, kicad, project_name, project_path))
    resp = _send(kicad, cmd, "kiapi.schematic.commands.SchematicHierarchyResponse")

    sheets: List[Dict[str, Any]] = []

    def walk(instance, depth: int) -> None:
        sheets.append({
            "name": instance.name or "(root)",
            "filename": instance.filename,
            "page": instance.page_number,
            "depth": depth,
        })
        for child in instance.children:
            walk(child, depth + 1)

    for top in resp.top_level_sheets:
        walk(top, 0)
    return sheets
