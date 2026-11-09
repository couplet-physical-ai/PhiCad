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
"""Total read/write control of the LIVE headless PhiCad IPC session, over kipy 0.8.0.

PROTOCOL
    session_worker.py <subcommand> '<json payload>'   ->  exactly ONE json object on stdout
    Exit code is always 0 (business failures are {"ok": false, ...}); nothing escapes as a
    traceback.  Runs under build/venv/bin/python with KICAD_API_SOCKET pointing at the managed
    `kicad-cli api-server`.  The MCP side (tools_session.py) never imports kipy.

SUBCOMMANDS
    read:  list_items get_item get_nets get_stackup bounding_box hit_test selection title_block
    write: update_footprint batch_move add_track add_via add_zone add_text add_graphic
           delete_items delete_by_filter set_net set_enabled_layers set_origin refill_zones
           run_action revert

UNITS
    Millimetres at the boundary (payload in, JSON out), nanometres inside.  1 mm = 1e6 nm.

THE TWO TRAPS THIS FILE EXISTS TO AVOID
    1. COPY SEMANTICS.  `fp.position` hands back a fresh Vector2 built from the proto, so
       `fp.position.x = ...` mutates a temporary and silently does nothing.  Every geometric
       write here ASSIGNS a whole object: `fp.position = Vector2.from_xy_mm(x, y)`.
    2. SNAPSHOTS.  A Board object is a snapshot plus a document handle; the items you hold do
       not refresh.  So every write is verified by re-reading through a FRESH
       kicad.get_board() and comparing; the result reports `applied` from that read-back, not
       from the fact that the RPC returned.

FORK COMPAT
    The fork emits kiapi.board.types.ReferencePoint children inside footprints, which kipy 0.8
    cannot unwrap (get_footprints() raises NotImplementedError).  install_fork_compat()
    registers a minimal wrapper, exactly as server.py's own session snippet does.

WHAT `ok` MEANS
    The operation ran AND the read-back proves it landed.  It does not mean the board is
    sensible: an added track connects nothing until its net matches, a zone is unfilled until
    refill_zones(), and NOTHING is on disk until session_save() (every write reports
    saved=false to say so).
"""

from __future__ import annotations

import json
import math
import sys

NM = 1000000.0  # nanometres per millimetre
TOL_NM = 1000   # 1 um: read-back tolerance for a position compare


def emit(payload):
    """Print the single JSON answer and leave. Always exit 0 (the payload carries ok)."""
    print(json.dumps(payload, default=str))
    sys.exit(0)


def fail(error, **extra):
    out = {"ok": False, "error": error}
    out.update(extra)
    emit(out)


def _exc(exc):
    return "%s: %s" % (type(exc).__name__, exc)


try:
    from kipy import KiCad
    from kipy.board_types import (
        ArcTrack, BoardCircle, BoardLayer, BoardPolygon, BoardRectangle, BoardSegment,
        BoardShape, BoardText, BoardTextBox, Dimension, Field, FootprintInstance, Group, Net,
        Pad, Track, Via, Zone)
    from kipy.common_types import TitleBlockInfo
    from kipy.geometry import Angle, PolygonWithHoles, PolyLine, PolyLineNode, Vector2
    from kipy.proto.board import board_commands_pb2
    from kipy.proto.board.board_types_pb2 import ZoneType
    from kipy.proto.common.types.base_types_pb2 import KIID
    from kipy.proto.common.commands import editor_commands_pb2
except Exception as exc:  # pragma: no cover - environment problem, reported as data
    emit({"ok": False, "error": "kipy import failed: %s" % _exc(exc),
          "hint": "phicad/bin/phicad venv (build/venv must carry kipy 0.8.0)"})


def install_fork_compat():
    """Teach kipy about the fork's ReferencePoint board item. Same shim as server.py.

    Duplicated on purpose: this worker must stand alone under the automation venv.
    """
    try:
        from kipy import board_types as _bt
        from kipy.proto.board import board_types_pb2
        from kipy.util import proto as _kproto
    except Exception:
        return False
    url = "type.googleapis.com/kiapi.board.types.ReferencePoint"
    if url in _kproto._any_urls:
        return False

    class ReferencePoint(_bt.BoardItem):
        def __init__(self, proto=None, proto_ref=None):
            self._proto = (proto_ref if proto_ref is not None
                           else board_types_pb2.ReferencePoint())
            if proto is not None:
                self._proto.CopyFrom(proto)

        @property
        def layer(self):
            return self._proto.layer

        @layer.setter
        def layer(self, value):
            self._proto.layer = value

    _kproto._any_urls[url] = board_types_pb2.ReferencePoint
    _bt._proto_to_object[board_types_pb2.ReferencePoint] = ReferencePoint
    return True


install_fork_compat()

# --------------------------------------------------------------------------------------
# session plumbing
# --------------------------------------------------------------------------------------

NO_DOC = "no document is open in the session"

#: What the server says when an RPC needs a GUI frame. A headless `kicad-cli api-server`
#: has no editor frame, so selection and TOOL_ACTION dispatch are simply not there.
HEADLESS = "not available in headless mode"


def headless_fail(what, exc, **extra):
    """Translate KiCad's 'not available in headless mode' into an honest answer."""
    if HEADLESS in str(exc):
        fail("%s is not available in a headless api-server (this RPC needs a GUI editor "
             "frame)" % what,
             headless=True,
             hint="open the board in the PhiCad GUI and point KICAD_API_SOCKET at it, or use "
                  "the equivalent headless tool (session_list_items / session_refill_zones)",
             kicad_error=str(exc)[:300], **extra)


def connect():
    try:
        kicad = KiCad()
        kicad.ping()
        return kicad
    except Exception as exc:
        fail("cannot reach api-server: %s" % _exc(exc),
             hint="api_server_start() then session_open('<abs path>.kicad_pcb')")


def get_board(kicad):
    """A FRESH board snapshot, or (None, why). Used for reads AND for write read-backs."""
    try:
        return kicad.get_board(), None
    except Exception as exc:
        msg = str(exc)
        if "GetOpenDocuments" in msg or "at least one board" in msg:
            return None, NO_DOC
        return None, _exc(exc)


def need_board(kicad):
    board, err = get_board(kicad)
    if board is None:
        fail(err, hint="session_open('<abs path>.kicad_pcb') first; a .kicad_pro alone does "
                       "NOT load a board")
    return board


# --------------------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------------------


def mm(nm):
    """nm -> mm, rounded to 4 decimals (0.1 um: finer than any board file records)."""
    return round(nm / NM, 4)


def nm(value_mm):
    return int(round(float(value_mm) * NM))


def xy_mm(vec):
    return [mm(vec.x), mm(vec.y)]


def point_arg(value, what):
    """Parse a [x_mm, y_mm] pair out of the payload (list or dict)."""
    if isinstance(value, dict):
        try:
            return float(value["x_mm"]), float(value["y_mm"])
        except Exception:
            fail("%s must be [x_mm, y_mm] or {x_mm, y_mm}" % what, got=value)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        fail("%s must be a [x_mm, y_mm] pair" % what, got=value)
    try:
        return float(value[0]), float(value[1])
    except Exception:
        fail("%s coordinates must be numbers" % what, got=value)


# --------------------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------------------


def canon_layer(idx):
    """BoardLayer enum value -> canonical KiCad layer name ('F.Cu')."""
    try:
        name = BoardLayer.Name(idx)
    except Exception:
        return str(idx)
    if name.startswith("BL_"):
        name = name[3:].replace("_", ".")
    return name


class Layers:
    """The board's enabled layers, with name resolution and the error payload for a typo."""

    def __init__(self, board):
        self.board = board
        try:
            self.enabled = list(board.get_enabled_layers())
        except Exception:
            self.enabled = []
        self.by_name = {}
        for idx in self.enabled:
            self.by_name[canon_layer(idx).upper()] = idx

    def names(self):
        return [canon_layer(i) for i in self.enabled]

    def label(self, idx):
        """The CANONICAL name ('F.Cu') — never the decorated form.

        Every layer string this worker emits must be accepted back as input, so a renamed
        layer is reported through user_name() as a separate field, never glued into the name.
        """
        return canon_layer(idx)

    def user_name(self, idx):
        """The board's own name for a layer when the user renamed it, else None."""
        canon = canon_layer(idx)
        try:
            own = self.board.get_layer_name(idx)
        except Exception:
            return None
        if own and own.lower() != canon.lower():
            return own
        return None

    def resolve(self, name, *, required=True):
        """Layer name -> enum value, validating against the ENABLED layers.

        Returns (idx, None) or (None, error_dict) that names every valid layer.
        """
        if name is None or name == "":
            if required:
                return None, {"ok": False, "error": "layer is required",
                              "valid_layers": self.names()}
            return None, None
        key = str(name).strip().upper()
        if key in self.by_name:
            return self.by_name[key], None
        # A user-renamed layer ('Component' for F.Cu): ask the board, then check it is enabled.
        try:
            idx = self.board.get_layer_by_name(str(name).strip())
        except Exception:
            idx = None
        if idx is not None and idx in self.enabled:
            return idx, None
        return None, {"ok": False,
                      "error": "layer %r is not an enabled layer on this board" % name,
                      "valid_layers": self.names()}


def need_layer(layers, name, *, required=True):
    idx, err = layers.resolve(name, required=required)
    if err:
        emit(err)
    return idx


# --------------------------------------------------------------------------------------
# nets
# --------------------------------------------------------------------------------------


def net_table(board):
    try:
        return {n.name: n for n in board.get_nets()}
    except Exception:
        return {}


def need_net(board, name, *, required=False):
    """Resolve a net NAME to a kipy Net. '' / None means the no-net net (allowed)."""
    if name is None or name == "":
        if required:
            fail("net is required")
        return None
    table = net_table(board)
    if name in table:
        return table[name]
    known = sorted(table)
    fail("no net named %r on this board" % name, net_count=len(known),
         known_nets=known[:200], nets_truncated=len(known) > 200)


# --------------------------------------------------------------------------------------
# item kinds and serialisation
# --------------------------------------------------------------------------------------

#: kind token -> the Board getter that returns it.  These are the nine buckets an agent
#: thinks in; kipy's own object types are an implementation detail underneath.
KINDS = ("footprint", "pad", "track", "via", "zone", "shape", "text", "dimension", "group")


def items_of_kind(board, kind):
    if kind == "footprint":
        return list(board.get_footprints())
    if kind == "pad":
        return list(board.get_pads())
    if kind == "track":
        return list(board.get_tracks())
    if kind == "via":
        return list(board.get_vias())
    if kind == "zone":
        return list(board.get_zones())
    if kind == "shape":
        return list(board.get_shapes())
    if kind == "text":
        return list(board.get_text())
    if kind == "dimension":
        return list(board.get_dimensions())
    if kind == "group":
        return list(board.get_groups())
    fail("unknown kind %r" % kind, valid_kinds=list(KINDS))


def kind_of(item):
    """Classify a kipy board item back into one of the KINDS tokens."""
    if isinstance(item, FootprintInstance):
        return "footprint"
    if isinstance(item, Pad):
        return "pad"
    if isinstance(item, (Track, ArcTrack)):
        return "track"
    if isinstance(item, Via):
        return "via"
    if isinstance(item, Zone):
        return "zone"
    if isinstance(item, BoardShape):
        return "shape"
    if isinstance(item, (BoardText, BoardTextBox, Field)):
        return "text"
    if isinstance(item, Dimension):
        return "dimension"
    if isinstance(item, Group):
        return "group"
    return type(item).__name__


def item_id(item):
    try:
        return item.id.value
    except Exception:
        return None


def item_layers(item):
    """Every layer index an item sits on (zones are multi-layer, vias span a range)."""
    out = []
    try:
        if isinstance(item, Zone):
            out = list(item.layers)
        elif isinstance(item, Via):
            drill = item.padstack.drill
            out = [drill.start_layer, drill.end_layer]
        elif isinstance(item, Pad):
            out = [lay.layer for lay in item.padstack.copper_layers]
        else:
            out = [item.layer]
    except Exception:
        out = []
    return [int(i) for i in out if isinstance(i, int)]


def item_net_name(item):
    try:
        return item.net.name
    except Exception:
        return None


def _shape_geometry(shape):
    """Geometry of a BoardShape, in mm, keyed by the concrete shape kind."""
    if isinstance(shape, BoardSegment):
        return {"shape": "segment", "start_mm": xy_mm(shape.start), "end_mm": xy_mm(shape.end)}
    if isinstance(shape, BoardCircle):
        return {"shape": "circle", "center_mm": xy_mm(shape.center),
                "radius_mm": round(shape.radius() / NM, 4)}
    if isinstance(shape, BoardRectangle):
        return {"shape": "rectangle", "top_left_mm": xy_mm(shape.top_left),
                "bottom_right_mm": xy_mm(shape.bottom_right)}
    if isinstance(shape, BoardPolygon):
        pts = []
        for poly in shape.polygons:
            for node in poly.outline:
                if node.has_point:
                    pts.append(xy_mm(node.point))
        return {"shape": "polygon", "points_mm": pts[:400], "point_count": len(pts)}
    kindname = type(shape).__name__
    geo = {"shape": kindname.replace("Board", "").lower()}
    for attr in ("start", "mid", "end"):
        try:
            geo["%s_mm" % attr] = xy_mm(getattr(shape, attr))
        except Exception:
            pass
    return geo


def item_json(item, layers, *, detail=False):
    """One board item as JSON: identity, layer(s), net, geometry. mm everywhere.

    `detail=True` adds the expensive/verbose parts (footprint pad list, zone outline,
    text attributes) — session_get_item uses it, session_list_items does not.
    """
    kind = kind_of(item)
    out = {"id": item_id(item), "kind": kind}
    lay = item_layers(item)
    if lay:
        if len(lay) == 1:
            out["layer"] = layers.label(lay[0])
            renamed = layers.user_name(lay[0])
            if renamed:
                out["layer_user_name"] = renamed
        else:
            out["layers"] = [layers.label(i) for i in lay]
    net = item_net_name(item)
    if net is not None:
        out["net"] = net
    try:
        out["locked"] = bool(item.locked)
    except Exception:
        pass

    if kind == "footprint":
        out["reference"] = item.reference_field.text.value
        out["value"] = item.value_field.text.value
        out["position_mm"] = xy_mm(item.position)
        out["orientation_deg"] = round(float(item.orientation.degrees), 3)
        try:
            out["side"] = "back" if canon_layer(item.layer) == "B.Cu" else "front"
        except Exception:
            pass
        if detail:
            out["datasheet"] = item.datasheet_field.text.value
            out["description"] = item.description_field.text.value
            try:
                out["footprint_id"] = str(item.definition.id)
            except Exception:
                pass
            pads = []
            for sub in item.definition.items:
                if isinstance(sub, Pad):
                    pads.append({"number": sub.number, "net": item_net_name(sub),
                                 "position_mm": xy_mm(sub.position), "id": item_id(sub)})
            out["pads"] = pads
            out["pad_count"] = len(pads)
            fields = {}
            for sub in item.definition.items:
                if isinstance(sub, Field) and sub.name:
                    fields[sub.name] = sub.text.value
            if fields:
                out["fields"] = fields
    elif kind == "pad":
        out["number"] = item.number
        out["position_mm"] = xy_mm(item.position)
        try:
            out["pad_type"] = int(item.pad_type)
        except Exception:
            pass
        if detail:
            try:
                out["drill_mm"] = mm(item.padstack.drill.diameter.x)
            except Exception:
                pass
    elif kind == "track":
        out["start_mm"] = xy_mm(item.start)
        out["end_mm"] = xy_mm(item.end)
        out["width_mm"] = mm(item.width)
        try:
            out["length_mm"] = mm(item.length())
        except Exception:
            pass
        if isinstance(item, ArcTrack):
            out["arc"] = True
            try:
                out["mid_mm"] = xy_mm(item.mid)
            except Exception:
                pass
    elif kind == "via":
        out["position_mm"] = xy_mm(item.position)
        try:
            out["drill_mm"] = mm(item.drill_diameter)
            out["diameter_mm"] = mm(item.diameter)
        except Exception:
            pass
        try:
            drill = item.padstack.drill
            out["start_layer"] = canon_layer(drill.start_layer)
            out["end_layer"] = canon_layer(drill.end_layer)
        except Exception:
            pass
    elif kind == "zone":
        out["name"] = item.name
        out["priority"] = int(item.priority)
        out["filled"] = bool(item.filled)
        out["is_rule_area"] = bool(item.is_rule_area())
        pts = []
        try:
            for node in item.outline.outline:
                if node.has_point:
                    pts.append(xy_mm(node.point))
        except Exception:
            pass
        out["outline_point_count"] = len(pts)
        if detail:
            out["outline_mm"] = pts[:400]
        else:
            out["outline_mm"] = pts[:8]
    elif kind == "shape":
        out.update(_shape_geometry(item))
        try:
            out["width_mm"] = mm(item.attributes.stroke.width)
        except Exception:
            pass
    elif kind == "text":
        if isinstance(item, Field):
            out["field_name"] = item.name
            out["text"] = item.text.value
            out["position_mm"] = xy_mm(item.text.position)
        else:
            try:
                out["text"] = item.value
            except Exception:
                out["text"] = None
            try:
                out["position_mm"] = xy_mm(item.position)
            except Exception:
                pass
        if detail:
            try:
                attrs = item.attributes if not isinstance(item, Field) \
                    else item.text.attributes
                out["size_mm"] = xy_mm(attrs.size)
                out["thickness_mm"] = mm(attrs.stroke_width)
                out["rotation_deg"] = round(float(attrs.angle), 3)
                out["bold"] = bool(attrs.bold)
                out["italic"] = bool(attrs.italic)
                out["visible"] = bool(attrs.visible)
            except Exception:
                pass
    elif kind == "dimension":
        try:
            out["text"] = item.text.value
        except Exception:
            pass
        for attr in ("start", "end"):
            try:
                out["%s_mm" % attr] = xy_mm(getattr(item, attr))
            except Exception:
                pass
    elif kind == "group":
        out["name"] = item.name
        try:
            out["member_ids"] = [k.value for k in item.proto.items]
            out["member_count"] = len(out["member_ids"])
        except Exception:
            pass
    return out


def find_by_ids(board, id_strings):
    """KIID strings -> (items, missing_ids). One RPC, order not guaranteed."""
    kiids = []
    for raw in id_strings:
        k = KIID()
        k.value = str(raw)
        kiids.append(k)
    try:
        found = list(board.get_items_by_id(kiids))
    except Exception as exc:
        # KiCad answers a wholly unknown id set with an error, not an empty list; that is a
        # normal "not found" for callers, not a transport failure.
        if "none of the requested IDs" in str(exc):
            found = []
        else:
            fail("get_items_by_id failed: %s" % _exc(exc), item_ids=list(id_strings))
    got = {item_id(i) for i in found}
    missing = [str(r) for r in id_strings if str(r) not in got]
    return found, missing


def find_footprint(board, ref):
    """The one footprint with this reference, or an error naming what the board has."""
    fps = list(board.get_footprints())
    hits = [fp for fp in fps if (fp.reference_field.text.value or "").upper()
            == str(ref).strip().upper()]
    if not hits:
        known = sorted({fp.reference_field.text.value for fp in fps
                        if fp.reference_field.text.value})
        fail("no footprint %r on the open board" % ref, footprint_count=len(fps),
             known_references=known[:200], references_truncated=len(known) > 200)
    if len(hits) > 1:
        fail("%d footprints share the reference %r" % (len(hits), ref))
    return hits[0]


# --------------------------------------------------------------------------------------
# write plumbing: commits and read-back verification
# --------------------------------------------------------------------------------------


class Commit:
    """begin_commit / push_commit / drop_commit as a context manager.

    A multi-item write must be ONE undo step, and must leave nothing half-applied when a
    later item blows up — so the commit is dropped on any exception and on emit-by-failure.
    `enabled=False` degrades to no commit at all (single-item writes do not need one).
    """

    def __init__(self, board, message, enabled=True):
        self.board = board
        self.message = message
        self.enabled = enabled
        self.handle = None
        self.state = "none"

    def __enter__(self):
        if self.enabled:
            try:
                self.handle = self.board.begin_commit()
                self.state = "open"
            except Exception as exc:
                self.state = "unavailable: %s" % _exc(exc)
                self.handle = None
        return self

    def push(self):
        if self.handle is None:
            return
        try:
            self.board.push_commit(self.handle, self.message)
            self.state = "pushed"
        except Exception as exc:
            self.state = "push failed: %s" % _exc(exc)
        self.handle = None

    def drop(self):
        if self.handle is None:
            return
        try:
            self.board.drop_commit(self.handle)
            self.state = "dropped"
        except Exception as exc:
            self.state = "drop failed: %s" % _exc(exc)
        self.handle = None

    def __exit__(self, exc_type, exc, tb):
        # SystemExit is how fail()/emit() leave: the payload is already a failure, so drop.
        if self.handle is not None:
            if exc_type is None:
                self.push()
            else:
                self.drop()
        return False


#: Appended to every write result: session state is not disk state.
SAVE_NOTE = ("changes live in the headless session only; session_save() writes them to the "
             "board file")


def write_result(payload, *, applied, commit=None, note=None):
    """Uniform write envelope: ok == applied, empty outputs/mutated, saved=false."""
    out = dict(payload)
    out["ok"] = bool(applied)
    out["applied"] = bool(applied)
    out["outputs"] = []
    out["mutated"] = []
    out["saved"] = False
    out["save_note"] = SAVE_NOTE
    if commit is not None:
        out["commit"] = commit.state
    if note:
        out["note"] = note
    if not applied and "error" not in out:
        out["error"] = "write did not apply (read-back through a fresh board differs)"
    emit(out)


def close(a, b, tol=TOL_NM):
    return abs(a - b) <= tol


def angle_close(a, b, tol=0.001):
    d = abs(float(a) - float(b)) % 360.0
    return min(d, 360.0 - d) <= tol


# --------------------------------------------------------------------------------------
# subcommand dispatch
# --------------------------------------------------------------------------------------

HANDLERS = {}


def handler(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


# --------------------------------------------------------------------------------------
# READ subcommands
# --------------------------------------------------------------------------------------


@handler("list_items")
def cmd_list_items(kicad, args):
    """kind + optional layer/net filter -> up to `limit` items, plus the unfiltered total."""
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in KINDS:
        fail("unknown kind %r" % kind, valid_kinds=list(KINDS))
    limit = int(args.get("limit") or 100)
    board = need_board(kicad)
    layers = Layers(board)
    want_layer = None
    if args.get("layer"):
        want_layer = need_layer(layers, args["layer"])
    want_net = args.get("net")
    if want_net:
        need_net(board, want_net)  # validates, listing known nets on a typo

    try:
        items = items_of_kind(board, kind)
    except SystemExit:
        raise
    except Exception as exc:
        fail("cannot list %ss: %s" % (kind, _exc(exc)))

    total = len(items)
    kept = []
    for item in items:
        if want_layer is not None and want_layer not in item_layers(item):
            continue
        if want_net and item_net_name(item) != want_net:
            continue
        kept.append(item)

    rows = []
    for item in kept[:limit]:
        try:
            rows.append(item_json(item, layers))
        except Exception as exc:
            rows.append({"id": item_id(item), "kind": kind, "error": _exc(exc)})
    emit({"ok": True, "kind": kind, "items": rows, "returned": len(rows),
          "matched": len(kept), "total_on_board": total,
          "truncated": len(kept) > len(rows),
          "filters": {"layer": args.get("layer"), "net": want_net, "limit": limit}})


@handler("get_item")
def cmd_get_item(kicad, args):
    """One item by KIID, in full detail (footprint pads/fields, zone outline, text attrs)."""
    item_ref = str(args.get("item_id") or "").strip()
    if not item_ref:
        fail("item_id is required")
    board = need_board(kicad)
    layers = Layers(board)
    found, missing = find_by_ids(board, [item_ref])
    if not found:
        fail("no item with id %r on the open board" % item_ref, missing=missing,
             hint="session_list_items(kind=...) to get ids")
    try:
        row = item_json(found[0], layers, detail=True)
    except Exception as exc:
        fail("cannot serialise item %r: %s" % (item_ref, _exc(exc)))
    emit({"ok": True, "item": row})


@handler("get_nets")
def cmd_get_nets(kicad, args):
    """Every net name, with net class; with_items adds per-net item counts (one RPC each)."""
    with_items = bool(args.get("with_items"))
    board = need_board(kicad)
    try:
        nets = list(board.get_nets())
    except Exception as exc:
        fail("get_nets failed: %s" % _exc(exc))
    classes = {}
    try:
        classes = {name: cls.name for name, cls in
                   board.get_netclass_for_nets(nets).items()}
    except Exception:
        classes = {}
    rows = []
    for net in nets:
        row = {"name": net.name}
        if net.name in classes:
            row["netclass"] = classes[net.name]
        if with_items:
            try:
                items = list(board.get_items_by_net(net))
                counts = {}
                for item in items:
                    key = kind_of(item)
                    counts[key] = counts.get(key, 0) + 1
                row["item_count"] = len(items)
                row["items_by_kind"] = counts
            except Exception as exc:
                row["items_error"] = _exc(exc)
        rows.append(row)
    rows.sort(key=lambda r: r["name"])
    emit({"ok": True, "nets": rows, "net_count": len(rows), "with_items": with_items})


@handler("get_stackup")
def cmd_get_stackup(kicad, args):
    """Physical stackup (layer, type, thickness, material) + enabled/copper layer counts."""
    board = need_board(kicad)
    layers = Layers(board)
    renamed = {layers.label(i): layers.user_name(i) for i in layers.enabled
               if layers.user_name(i)}
    out = {"ok": True, "enabled_layers": layers.names(),
           "enabled_layer_count": len(layers.enabled),
           "renamed_layers": renamed}
    try:
        out["copper_layer_count"] = board.get_copper_layer_count()
    except Exception as exc:
        out["copper_layer_count_error"] = _exc(exc)
    try:
        stackup = board.get_stackup()
    except Exception as exc:
        out["stackup_error"] = _exc(exc)
        emit(out)
    rows = []
    total = 0
    for lay in stackup.layers:
        row = {"layer": canon_layer(lay.layer), "type": int(lay.type),
               "thickness_mm": round(lay.thickness / NM, 5),
               "material": lay.material_name, "user_name": lay.user_name,
               "enabled": bool(lay.enabled)}
        if lay.enabled:
            total += lay.thickness
        rows.append(row)
    out["layers"] = rows
    out["layer_count"] = len(rows)
    out["board_thickness_mm"] = round(total / NM, 5)
    try:
        out["finish"] = stackup.finish.type_name
    except Exception:
        pass
    try:
        out["impedance_controlled"] = bool(stackup.impedance.is_controlled)
    except Exception:
        pass
    emit(out)


@handler("bounding_box")
def cmd_bounding_box(kicad, args):
    """KiCad's own bbox for the named items, or the union over everything on the board."""
    ids = args.get("item_ids")
    board = need_board(kicad)
    layers = Layers(board)
    if ids:
        items, missing = find_by_ids(board, [str(i) for i in ids])
        scope = "item_ids"
    else:
        items = []
        for kind in KINDS:
            try:
                items.extend(items_of_kind(board, kind))
            except Exception:
                continue
        missing = []
        scope = "whole_board"
    if not items:
        fail("no items to measure", scope=scope, missing=missing)

    try:
        boxes = board.get_item_bounding_box(items)
    except Exception as exc:
        fail("get_item_bounding_box failed: %s" % _exc(exc))
    if not isinstance(boxes, list):
        boxes = [boxes]

    per_item = []
    x0 = y0 = x1 = y1 = None
    for item, box in zip(items, boxes):
        if box is None:
            per_item.append({"id": item_id(item), "kind": kind_of(item), "box": None})
            continue
        bx0, by0 = box.pos.x, box.pos.y
        bx1, by1 = bx0 + box.size.x, by0 + box.size.y
        x0 = bx0 if x0 is None else min(x0, bx0)
        y0 = by0 if y0 is None else min(y0, by0)
        x1 = bx1 if x1 is None else max(x1, bx1)
        y1 = by1 if y1 is None else max(y1, by1)
        per_item.append({"id": item_id(item), "kind": kind_of(item),
                         "x_mm": mm(bx0), "y_mm": mm(by0),
                         "width_mm": mm(box.size.x), "height_mm": mm(box.size.y)})
    if x0 is None:
        fail("every item returned an empty bounding box", scope=scope,
             measured=len(per_item))
    out = {"ok": True, "scope": scope, "measured": len(per_item),
           "bounding_box_mm": {"x": mm(x0), "y": mm(y0), "width": mm(x1 - x0),
                               "height": mm(y1 - y0), "x2": mm(x1), "y2": mm(y1),
                               "center": [mm((x0 + x1) // 2), mm((y0 + y1) // 2)]}}
    if missing:
        out["missing_ids"] = missing
    if ids:
        out["items"] = per_item
    else:
        out["layer_count"] = len(layers.enabled)
    emit(out)


@handler("hit_test")
def cmd_hit_test(kicad, args):
    """What is under (x_mm, y_mm): asks KiCad hit_test per candidate item.

    Filtering by layer first is what keeps this to a sane number of RPCs; without a layer
    the whole board is probed and that is slow on a big design (one round trip per item).
    """
    if args.get("x_mm") is None or args.get("y_mm") is None:
        fail("x_mm and y_mm are required")
    x, y = float(args["x_mm"]), float(args["y_mm"])
    tol_mm = float(args.get("tolerance_mm") or 0.0)
    board = need_board(kicad)
    layers = Layers(board)
    want_layer = None
    if args.get("layer"):
        want_layer = need_layer(layers, args["layer"])

    candidates = []
    for kind in KINDS:
        if kind == "group":
            continue
        try:
            for item in items_of_kind(board, kind):
                if want_layer is not None and want_layer not in item_layers(item):
                    continue
                candidates.append(item)
        except Exception:
            continue

    pos = Vector2.from_xy_mm(x, y)
    tol = nm(tol_mm)
    hits = []
    errors = 0
    for item in candidates:
        try:
            if board.hit_test(item, pos, tol):
                hits.append(item_json(item, layers))
        except Exception:
            errors += 1
    out = {"ok": True, "at_mm": [x, y], "tolerance_mm": tol_mm,
           "layer": args.get("layer"), "tested": len(candidates), "hits": hits,
           "hit_count": len(hits)}
    if errors:
        out["untestable_items"] = errors
    emit(out)


@handler("selection")
def cmd_selection(kicad, args):
    """get / set / add / clear the session selection; always returns the resulting set."""
    action = str(args.get("action") or "get").strip().lower()
    if action not in ("get", "set", "add", "clear"):
        fail("unknown selection action %r" % action, valid=["get", "set", "add", "clear"])
    board = need_board(kicad)
    layers = Layers(board)
    ids = [str(i) for i in (args.get("item_ids") or [])]
    missing = []

    try:
        if action == "clear":
            board.clear_selection()
        elif action in ("set", "add"):
            if not ids:
                fail("item_ids is required for action %r" % action)
            items, missing = find_by_ids(board, ids)
            if not items:
                fail("none of the given item_ids exist on this board", missing=missing)
            if action == "set":
                board.clear_selection()
            board.add_to_selection(list(items))
        current = list(board.get_selection())
    except SystemExit:
        raise
    except Exception as exc:
        headless_fail("selection", exc, action=action)
        fail("selection %s failed: %s" % (action, _exc(exc)))

    rows = []
    for item in current:
        try:
            rows.append(item_json(item, layers))
        except Exception as exc:
            rows.append({"id": item_id(item), "error": _exc(exc)})
    out = {"ok": True, "action": action, "selection": rows, "selected_count": len(rows)}
    if missing:
        out["missing_ids"] = missing
    if action != "get":
        out["outputs"] = []
        out["mutated"] = []
        out["saved"] = False
        out["save_note"] = "selection is session-only state and is never written to disk"
    emit(out)


#: Title block keys an agent may set. `comments` takes {"1": "...", ...}.
TITLE_FIELDS = ("title", "date", "revision", "company")


@handler("title_block")
def cmd_title_block(kicad, args):
    """Read the title block, or write the given fields and read them back to verify."""
    fields = args.get("fields")
    board = need_board(kicad)
    try:
        tb = board.get_title_block_info()
    except Exception as exc:
        fail("get_title_block_info failed: %s" % _exc(exc))

    def snapshot(info):
        row = {k: getattr(info, k) for k in TITLE_FIELDS}
        row["comments"] = {str(k): v for k, v in info.comments.items() if v}
        return row

    before = snapshot(tb)
    if not fields:
        emit({"ok": True, "title_block": before})

    if not isinstance(fields, dict):
        fail("fields must be an object of {title,date,revision,company,comments}")
    unknown = [k for k in fields if k not in TITLE_FIELDS and k != "comments"]
    if unknown:
        fail("unknown title block fields: %s" % ", ".join(sorted(unknown)),
             valid_fields=list(TITLE_FIELDS) + ["comments"])

    for key in TITLE_FIELDS:
        if key in fields:
            setattr(tb, key, str(fields[key]))
    if "comments" in fields:
        comments = fields["comments"]
        if not isinstance(comments, dict):
            fail("fields.comments must be an object like {\"1\": \"text\"}")
        tb.comments = {int(k): str(v) for k, v in comments.items()}

    try:
        board.set_title_block_info(tb)
    except Exception as exc:
        fail("set_title_block_info failed: %s" % _exc(exc), before=before)

    fresh = need_board(kicad)
    try:
        after = snapshot(fresh.get_title_block_info())
    except Exception as exc:
        fail("wrote the title block but could not read it back: %s" % _exc(exc),
             before=before)

    wanted = {}
    for key in TITLE_FIELDS:
        if key in fields:
            wanted[key] = str(fields[key])
    applied = all(after.get(k) == v for k, v in wanted.items())
    if "comments" in fields:
        for k, v in fields["comments"].items():
            if after["comments"].get(str(int(k))) != str(v):
                applied = False
    write_result({"before": before, "after": after, "requested": fields},
                 applied=applied)


# --------------------------------------------------------------------------------------
# WRITE subcommands — footprints
# --------------------------------------------------------------------------------------


def fp_snapshot(fp, layers):
    return {"reference": fp.reference_field.text.value,
            "value": fp.value_field.text.value,
            "position_mm": xy_mm(fp.position),
            "rotation_deg": round(float(fp.orientation.degrees), 3),
            "layer": layers.label(fp.layer),
            "side": "back" if canon_layer(fp.layer) == "B.Cu" else "front",
            "locked": bool(fp.locked),
            "id": item_id(fp)}


def fp_fields(fp):
    """Custom footprint fields (everything past reference/value/datasheet/description)."""
    out = {}
    for sub in fp.definition.items:
        if isinstance(sub, Field) and sub.name:
            out[sub.name] = sub.text.value
    return out


@handler("update_footprint")
def cmd_update_footprint(kicad, args):
    """Position / rotation / side / lock / value / reference / custom fields, in one commit.

    `side` is applied through flip_items (KiCad's own mirror), never by writing .layer — the
    layer setter moves the footprint's own layer without mirroring its children, which
    produces a board that looks placed and is unmanufacturable.
    """
    ref = str(args.get("ref") or "").strip()
    if not ref:
        fail("ref is required")
    board = need_board(kicad)
    layers = Layers(board)
    fp = find_footprint(board, ref)
    before = fp_snapshot(fp, layers)
    before_fields = fp_fields(fp)

    x = args.get("x_mm")
    y = args.get("y_mm")
    rot = args.get("rot_deg")
    side = args.get("side")
    locked = args.get("locked")
    value = args.get("value")
    new_ref = args.get("reference")
    fields = args.get("fields")
    if all(v is None for v in (x, y, rot, side, locked, value, new_ref, fields)):
        fail("nothing to change: pass at least one of x_mm, y_mm, rot_deg, side, locked, "
             "value, reference, fields", current=before, fields=before_fields)
    if side is not None:
        side = str(side).strip().lower()
        if side not in ("front", "back", "top", "bottom"):
            fail("side must be front|back (top/bottom accepted)", got=args.get("side"))
        side = {"top": "front", "bottom": "back"}[side] if side in ("top", "bottom") else side

    requested = {}
    unknown_fields = []
    if fields is not None:
        if not isinstance(fields, dict):
            fail("fields must be an object of {field_name: value}")
        have = set(before_fields)
        unknown_fields = [k for k in fields if k not in have]
        if unknown_fields:
            fail("footprint %s has no field(s): %s" % (ref, ", ".join(sorted(unknown_fields))),
                 known_fields=sorted(have),
                 hint="only existing footprint fields can be set through the IPC API")

    # ---- the flip goes FIRST ----
    # MEASURED (pic_programmer, fork 10.99.0), two facts that fix the order of operations:
    #   1. FlipItems issued INSIDE an open commit is accepted and then silently discarded (the
    #      read-back still shows the old side), so the flip cannot share the property commit.
    #   2. Flipping MIRRORS the footprint, which also changes its orientation (0 -> 180 deg).
    # So flip first, then re-read, then write properties: a caller asking for side='back' AND
    # rot_deg=0 gets a back-side footprint at 0 deg, not the flip's 180.
    flipped_side = False
    if side is not None:
        requested["side"] = side
        if side != before["side"]:
            try:
                direction = board_commands_pb2.BoardFlipDirection.BFD_LEFT_RIGHT
                done = board.flip_items_by_id(fp.id, direction)
            except Exception as exc:
                fail("flip_items failed: %s" % _exc(exc), before=before,
                     other_changes_applied=[])
            if not done:
                fail("KiCad refused to flip %s" % ref, before=before,
                     other_changes_applied=[])
            flipped_side = True
            # The handle in hand is stale after the flip; re-read it or the property write
            # pushes the pre-flip geometry back over KiCad's mirrored copy.
            board = need_board(kicad)
            layers = Layers(board)
            fp = find_footprint(board, ref)

    # ---- mutate the snapshot (assignment only: the getters hand back copies) ----
    if x is not None or y is not None:
        nx = float(x) if x is not None else mm(fp.position.x)
        ny = float(y) if y is not None else mm(fp.position.y)
        fp.position = Vector2.from_xy_mm(nx, ny)
        requested["position_mm"] = [nx, ny]
    if rot is not None:
        fp.orientation = Angle.from_degrees(float(rot))
        requested["rotation_deg"] = float(rot)
    if locked is not None:
        fp.locked = bool(locked)
        requested["locked"] = bool(locked)
    if value is not None:
        fp.value_field.text.value = str(value)
        requested["value"] = str(value)
    if new_ref is not None:
        fp.reference_field.text.value = str(new_ref)
        requested["reference"] = str(new_ref)
    if fields:
        items = list(fp.definition.items)
        for sub in items:
            if isinstance(sub, Field) and sub.name in fields:
                sub.text.value = str(fields[sub.name])
        fp.definition.items = items
        requested["fields"] = {k: str(v) for k, v in fields.items()}

    property_writes = [k for k in requested if k != "side"]
    with Commit(board, "session_update_footprint %s" % ref,
                enabled=len(property_writes) > 1) as commit:
        if property_writes:
            try:
                board.update_items(fp)
            except Exception as exc:
                commit.drop()
                fail("update_items failed: %s" % _exc(exc), before=before,
                     commit=commit.state, side_flipped=flipped_side)
        commit.push()

    # ---- verify through a FRESH board ----
    fresh = need_board(kicad)
    fresh_layers = Layers(fresh)
    look_for = requested.get("reference", before["reference"])
    got = find_footprint(fresh, look_for)
    after = fp_snapshot(got, fresh_layers)
    after_fields = fp_fields(got)

    problems = []
    if "position_mm" in requested:
        wx, wy = requested["position_mm"]
        if not (close(nm(after["position_mm"][0]), nm(wx))
                and close(nm(after["position_mm"][1]), nm(wy))):
            problems.append("position")
    if "rotation_deg" in requested and not angle_close(after["rotation_deg"],
                                                      requested["rotation_deg"]):
        problems.append("rotation")
    if "side" in requested and after["side"] != requested["side"]:
        problems.append("side")
    if "locked" in requested and after["locked"] != requested["locked"]:
        problems.append("locked")
    if "value" in requested and after["value"] != requested["value"]:
        problems.append("value")
    if "reference" in requested and after["reference"] != requested["reference"]:
        problems.append("reference")
    for key, want in (requested.get("fields") or {}).items():
        if after_fields.get(key) != want:
            problems.append("field:%s" % key)

    payload = {"reference": after["reference"], "requested": requested, "before": before,
               "after": after}
    if requested.get("fields"):
        payload["fields_before"] = before_fields
        payload["fields_after"] = after_fields
    if problems:
        payload["unapplied"] = problems
    if flipped_side:
        payload["undo_steps"] = 2 if property_writes else 1
        payload["flip_note"] = ("side changes go through KiCad's own FlipItems as a separate "
                                "transaction (mirroring also changes orientation), so this "
                                "call is a flip followed by the property write")
    write_result(payload, applied=not problems, commit=commit)


@handler("batch_move")
def cmd_batch_move(kicad, args):
    """Many footprints to absolute positions (and optional rotations) in ONE undo step.

    moves: [{"ref": "R1", "x_mm": 10, "y_mm": 20, "rot_deg": 90}, ...]  — x/y/rot each
    optional per row, so a row can rotate without moving.  Either every row lands or the
    commit is dropped and the board is untouched.
    """
    moves = args.get("moves")
    if not isinstance(moves, list) or not moves:
        fail("moves must be a non-empty list of {ref, x_mm, y_mm, rot_deg}")
    board = need_board(kicad)
    layers = Layers(board)

    plan = []
    for row in moves:
        if not isinstance(row, dict):
            fail("each move must be an object", got=row)
        ref = str(row.get("ref") or "").strip()
        if not ref:
            fail("each move needs a ref", got=row)
        if row.get("x_mm") is None and row.get("y_mm") is None and row.get("rot_deg") is None:
            fail("move for %s changes nothing (needs x_mm/y_mm/rot_deg)" % ref, got=row)
        fp = find_footprint(board, ref)
        before = fp_snapshot(fp, layers)
        nx = float(row["x_mm"]) if row.get("x_mm") is not None else before["position_mm"][0]
        ny = float(row["y_mm"]) if row.get("y_mm") is not None else before["position_mm"][1]
        fp.position = Vector2.from_xy_mm(nx, ny)
        want = {"ref": ref, "position_mm": [nx, ny]}
        if row.get("rot_deg") is not None:
            deg = float(row["rot_deg"])
            fp.orientation = Angle.from_degrees(deg)
            want["rotation_deg"] = deg
        plan.append((fp, before, want))

    with Commit(board, "session_batch_move (%d footprints)" % len(plan)) as commit:
        try:
            board.update_items([fp for fp, _, _ in plan])
        except Exception as exc:
            commit.drop()
            fail("update_items failed for the batch: %s" % _exc(exc), commit=commit.state,
                 requested=[w for _, _, w in plan])
        commit.push()

    fresh = need_board(kicad)
    fresh_layers = Layers(fresh)
    rows = []
    failed = []
    for _, before, want in plan:
        got = find_footprint(fresh, want["ref"])
        after = fp_snapshot(got, fresh_layers)
        ok = (close(nm(after["position_mm"][0]), nm(want["position_mm"][0]))
              and close(nm(after["position_mm"][1]), nm(want["position_mm"][1])))
        if "rotation_deg" in want and not angle_close(after["rotation_deg"],
                                                     want["rotation_deg"]):
            ok = False
        if not ok:
            failed.append(want["ref"])
        rows.append({"ref": want["ref"], "requested": want,
                     "before_mm": before["position_mm"], "after_mm": after["position_mm"],
                     "after_rot_deg": after["rotation_deg"], "applied": ok})
    payload = {"moves": rows, "requested_count": len(plan),
               "applied_count": len(plan) - len(failed)}
    if failed:
        payload["unapplied_refs"] = failed
    write_result(payload, applied=not failed, commit=commit)


# --------------------------------------------------------------------------------------
# WRITE subcommands — creation
# --------------------------------------------------------------------------------------


def create_and_verify(kicad, board, items, message, extra=None):
    """create_items() in a commit, then prove each new item exists on a FRESH board.

    Returns via write_result: `created` carries the KIIDs KiCad assigned and the read-back
    of each item, so the caller can address them immediately (session_get_item, delete...).
    """
    with Commit(board, message, enabled=len(items) > 1) as commit:
        try:
            created = list(board.create_items(items))
        except Exception as exc:
            commit.drop()
            fail("create_items failed: %s" % _exc(exc), commit=commit.state)
        if not created:
            commit.drop()
            fail("KiCad created nothing (the item was rejected)", commit=commit.state,
                 hint="check the layer is enabled and the geometry is non-degenerate")
        commit.push()

    ids = [item_id(i) for i in created]
    fresh = need_board(kicad)
    layers = Layers(fresh)
    found, missing = find_by_ids(fresh, [i for i in ids if i])
    rows = []
    for item in found:
        try:
            rows.append(item_json(item, layers, detail=True))
        except Exception as exc:
            rows.append({"id": item_id(item), "error": _exc(exc)})
    payload = {"created_ids": ids, "created": rows, "created_count": len(rows)}
    if missing:
        payload["missing_after_create"] = missing
    if extra:
        payload.update(extra)
    write_result(payload, applied=bool(rows) and not missing, commit=commit)


@handler("add_track")
def cmd_add_track(kicad, args):
    """One straight copper track segment: start/end in mm, layer name, width in mm."""
    board = need_board(kicad)
    layers = Layers(board)
    sx, sy = point_arg(args.get("start_mm"), "start_mm")
    ex, ey = point_arg(args.get("end_mm"), "end_mm")
    layer = need_layer(layers, args.get("layer"))
    width = args.get("width_mm")
    if width is None or float(width) <= 0:
        fail("width_mm must be a positive number")
    if close(nm(sx), nm(ex)) and close(nm(sy), nm(ey)):
        fail("start_mm and end_mm are the same point (zero-length track)")
    net = need_net(board, args.get("net"))

    track = Track()
    track.start = Vector2.from_xy_mm(sx, sy)
    track.end = Vector2.from_xy_mm(ex, ey)
    track.layer = layer
    track.width = nm(width)
    if net is not None:
        track.net = net
    create_and_verify(kicad, board, [track], "session_add_track",
                      extra={"requested": {"start_mm": [sx, sy], "end_mm": [ex, ey],
                                           "layer": canon_layer(layer),
                                           "width_mm": float(width),
                                           "net": args.get("net") or None}})


@handler("add_via")
def cmd_add_via(kicad, args):
    """One via: position, drill and pad diameter in mm, and the copper span it drills."""
    board = need_board(kicad)
    layers = Layers(board)
    x, y = point_arg(args.get("at_mm"), "at_mm")
    drill = args.get("drill_mm")
    size = args.get("size_mm")
    if drill is None or float(drill) <= 0:
        fail("drill_mm must be a positive number")
    if size is None or float(size) <= 0:
        fail("size_mm must be a positive number")
    if float(size) <= float(drill):
        fail("size_mm (%s) must be larger than drill_mm (%s)" % (size, drill))
    start_layer = need_layer(layers, args.get("start_layer") or "F.Cu")
    end_layer = need_layer(layers, args.get("end_layer") or "B.Cu")
    net = need_net(board, args.get("net"))

    via = Via()
    via.position = Vector2.from_xy_mm(x, y)
    via.diameter = nm(size)
    via.drill_diameter = nm(drill)
    via.padstack.drill.start_layer = start_layer
    via.padstack.drill.end_layer = end_layer
    if net is not None:
        via.net = net
    create_and_verify(kicad, board, [via], "session_add_via",
                      extra={"requested": {"at_mm": [x, y], "drill_mm": float(drill),
                                           "size_mm": float(size),
                                           "start_layer": canon_layer(start_layer),
                                           "end_layer": canon_layer(end_layer),
                                           "net": args.get("net") or None}})


@handler("add_zone")
def cmd_add_zone(kicad, args):
    """A copper zone (or an unnetted graphical area) from a closed polygon of mm points.

    The zone is created UNFILLED — call session_refill_zones() to pour copper into it, or it
    contributes nothing to connectivity and shows up in DRC as an empty island.
    """
    board = need_board(kicad)
    layers = Layers(board)
    poly = args.get("polygon_mm")
    if not isinstance(poly, list) or len(poly) < 3:
        fail("polygon_mm must be a list of at least 3 [x_mm, y_mm] points")
    layer = need_layer(layers, args.get("layer"))
    net = need_net(board, args.get("net"))
    priority = int(args.get("priority") or 0)
    if priority < 0:
        fail("priority must be >= 0")

    points = [point_arg(p, "polygon_mm point") for p in poly]
    outline = PolyLine()
    for px, py in points:
        outline.append(PolyLineNode.from_point(Vector2.from_xy_mm(px, py)))
    outline.closed = True
    shape = PolygonWithHoles()
    shape.outline = outline

    zone = Zone()
    zone.type = ZoneType.ZT_COPPER
    zone.layers = [layer]
    zone.outline = shape
    zone.priority = priority
    if args.get("name"):
        zone.name = str(args["name"])
    if net is not None:
        zone.net = net
    create_and_verify(kicad, board, [zone], "session_add_zone",
                      extra={"requested": {"layer": canon_layer(layer),
                                           "point_count": len(points),
                                           "net": args.get("net") or None,
                                           "name": args.get("name") or None,
                                           "priority": priority},
                             "fill_note": "the new zone is UNFILLED; call "
                                          "session_refill_zones() to pour it"})


@handler("add_text")
def cmd_add_text(kicad, args):
    """Free text on any layer: value, position, glyph size, stroke thickness, rotation."""
    board = need_board(kicad)
    layers = Layers(board)
    text = args.get("text")
    if text is None or str(text) == "":
        fail("text is required and must be non-empty")
    x, y = point_arg(args.get("at_mm"), "at_mm")
    layer = need_layer(layers, args.get("layer"))
    size = float(args.get("size_mm") or 1.0)
    if size <= 0:
        fail("size_mm must be a positive number")
    thickness = args.get("thickness_mm")
    if thickness is None:
        thickness = round(size * 0.15, 4)   # KiCad's own default ratio for stroke fonts
    if float(thickness) <= 0:
        fail("thickness_mm must be a positive number")
    rotation = float(args.get("rotation_deg") or 0.0)

    item = BoardText()
    item.value = str(text)
    item.layer = layer
    item.position = Vector2.from_xy_mm(x, y)
    attrs = item.attributes
    attrs.size = Vector2.from_xy_mm(size, size)
    attrs.stroke_width = nm(thickness)
    attrs.angle = rotation
    item.attributes = attrs
    create_and_verify(kicad, board, [item], "session_add_text",
                      extra={"requested": {"text": str(text), "at_mm": [x, y],
                                           "layer": canon_layer(layer), "size_mm": size,
                                           "thickness_mm": float(thickness),
                                           "rotation_deg": rotation}})


def _arc_mid(sx, sy, ex, ey, cx, cy):
    """Midpoint of the arc through start/end about center — KiCad stores arcs start/mid/end."""
    r = math.hypot(sx - cx, sy - cy)
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    # Take the short way round, which is what a caller giving start/end/center means.
    if a1 - a0 > math.pi:
        a1 -= 2 * math.pi
    elif a0 - a1 > math.pi:
        a1 += 2 * math.pi
    am = (a0 + a1) / 2.0
    return cx + r * math.cos(am), cy + r * math.sin(am)


GRAPHIC_KINDS = ("line", "rect", "circle", "arc", "poly")


@handler("add_graphic")
def cmd_add_graphic(kicad, args):
    """A graphic shape (line|rect|circle|arc|poly) on any layer, stroke width in mm.

    Which arguments matter depends on `kind`:
      line  start_mm + end_mm            rect   start_mm + end_mm (opposite corners)
      circle center_mm + radius_mm       arc    start_mm + end_mm + center_mm
      poly   points_mm (>= 3 points)
    """
    board = need_board(kicad)
    layers = Layers(board)
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in GRAPHIC_KINDS:
        fail("unknown graphic kind %r" % kind, valid_kinds=list(GRAPHIC_KINDS))
    layer = need_layer(layers, args.get("layer"))
    width = args.get("width_mm")
    if width is None or float(width) <= 0:
        fail("width_mm must be a positive number")

    requested = {"kind": kind, "layer": canon_layer(layer), "width_mm": float(width)}

    if kind == "line":
        sx, sy = point_arg(args.get("start_mm"), "start_mm")
        ex, ey = point_arg(args.get("end_mm"), "end_mm")
        shape = BoardSegment()
        shape.start = Vector2.from_xy_mm(sx, sy)
        shape.end = Vector2.from_xy_mm(ex, ey)
        requested.update({"start_mm": [sx, sy], "end_mm": [ex, ey]})
    elif kind == "rect":
        sx, sy = point_arg(args.get("start_mm"), "start_mm")
        ex, ey = point_arg(args.get("end_mm"), "end_mm")
        if close(nm(sx), nm(ex)) or close(nm(sy), nm(ey)):
            fail("rect needs two opposite corners with non-zero width and height")
        shape = BoardRectangle()
        shape.top_left = Vector2.from_xy_mm(min(sx, ex), min(sy, ey))
        shape.bottom_right = Vector2.from_xy_mm(max(sx, ex), max(sy, ey))
        requested.update({"top_left_mm": [min(sx, ex), min(sy, ey)],
                          "bottom_right_mm": [max(sx, ex), max(sy, ey)]})
    elif kind == "circle":
        cx, cy = point_arg(args.get("center_mm"), "center_mm")
        radius = args.get("radius_mm")
        if radius is None or float(radius) <= 0:
            fail("circle needs a positive radius_mm")
        shape = BoardCircle()
        shape.center = Vector2.from_xy_mm(cx, cy)
        shape.radius_point = Vector2.from_xy_mm(cx + float(radius), cy)
        requested.update({"center_mm": [cx, cy], "radius_mm": float(radius)})
    elif kind == "arc":
        sx, sy = point_arg(args.get("start_mm"), "start_mm")
        ex, ey = point_arg(args.get("end_mm"), "end_mm")
        cx, cy = point_arg(args.get("center_mm"), "center_mm")
        mx, my = _arc_mid(sx, sy, ex, ey, cx, cy)
        from kipy.board_types import BoardArc
        shape = BoardArc()
        shape.start = Vector2.from_xy_mm(sx, sy)
        shape.mid = Vector2.from_xy_mm(mx, my)
        shape.end = Vector2.from_xy_mm(ex, ey)
        requested.update({"start_mm": [sx, sy], "end_mm": [ex, ey],
                          "center_mm": [cx, cy], "mid_mm": [round(mx, 4), round(my, 4)]})
    else:  # poly
        pts = args.get("points_mm")
        if not isinstance(pts, list) or len(pts) < 3:
            fail("poly needs points_mm with at least 3 [x_mm, y_mm] points")
        points = [point_arg(p, "points_mm point") for p in pts]
        outline = PolyLine()
        for px, py in points:
            outline.append(PolyLineNode.from_point(Vector2.from_xy_mm(px, py)))
        outline.closed = True
        poly = PolygonWithHoles()
        poly.outline = outline
        shape = BoardPolygon()
        shape.polygons.append(poly)
        requested["point_count"] = len(points)

    shape.layer = layer
    attrs = shape.attributes
    attrs.stroke.width = nm(width)
    shape.attributes = attrs
    create_and_verify(kicad, board, [shape], "session_add_graphic %s" % kind,
                      extra={"requested": requested})


# --------------------------------------------------------------------------------------
# WRITE subcommands — deletion, nets, board-level settings
# --------------------------------------------------------------------------------------


def delete_and_verify(kicad, board, items, message, extra=None):
    """remove_items() in a commit, then prove the ids are GONE from a fresh board."""
    ids = [item_id(i) for i in items if item_id(i)]
    with Commit(board, message, enabled=len(items) > 1) as commit:
        try:
            board.remove_items(list(items))
        except Exception as exc:
            commit.drop()
            fail("remove_items failed: %s" % _exc(exc), commit=commit.state,
                 item_ids=ids)
        commit.push()

    fresh = need_board(kicad)
    still, _ = find_by_ids(fresh, ids)
    survivors = [item_id(i) for i in still]
    payload = {"requested_ids": ids, "deleted_count": len(ids) - len(survivors)}
    if survivors:
        payload["still_present"] = survivors
    if extra:
        payload.update(extra)
    write_result(payload, applied=not survivors, commit=commit)


@handler("delete_items")
def cmd_delete_items(kicad, args):
    """Delete the named items by KIID. Unknown ids are reported, not silently ignored."""
    ids = [str(i) for i in (args.get("item_ids") or [])]
    if not ids:
        fail("item_ids is required (a list of KIID strings)")
    board = need_board(kicad)
    items, missing = find_by_ids(board, ids)
    if not items:
        fail("none of the given item_ids exist on this board", missing=missing,
             hint="session_list_items(kind=...) to get current ids")
    extra = {"missing_ids": missing} if missing else None
    delete_and_verify(kicad, board, items, "session_delete_items (%d)" % len(items), extra)


@handler("delete_by_filter")
def cmd_delete_by_filter(kicad, args):
    """Delete every item of a kind (optionally narrowed by layer/net). DRY RUN by default.

    dry_run=True (the default) only reports what WOULD go, because 'delete every track on
    F.Cu' is one keystroke away from deleting a whole board's routing.
    """
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in KINDS:
        fail("unknown kind %r" % kind, valid_kinds=list(KINDS))
    dry_run = args.get("dry_run")
    dry_run = True if dry_run is None else bool(dry_run)
    board = need_board(kicad)
    layers = Layers(board)
    want_layer = None
    if args.get("layer"):
        want_layer = need_layer(layers, args["layer"])
    want_net = args.get("net")
    if want_net:
        need_net(board, want_net)

    victims = []
    for item in items_of_kind(board, kind):
        if want_layer is not None and want_layer not in item_layers(item):
            continue
        if want_net and item_net_name(item) != want_net:
            continue
        victims.append(item)

    preview = []
    for item in victims[:50]:
        try:
            preview.append(item_json(item, layers))
        except Exception:
            preview.append({"id": item_id(item), "kind": kind})
    filters = {"kind": kind, "layer": args.get("layer"), "net": want_net}

    if dry_run:
        emit({"ok": True, "dry_run": True, "filters": filters,
              "would_delete": len(victims), "sample": preview,
              "sample_truncated": len(victims) > len(preview),
              "outputs": [], "mutated": [], "saved": False,
              "note": "nothing was deleted; call again with dry_run=false to apply"})
    if not victims:
        emit({"ok": True, "dry_run": False, "filters": filters, "deleted_count": 0,
              "outputs": [], "mutated": [], "saved": False,
              "note": "no item matched the filter; nothing to delete"})
    delete_and_verify(kicad, board, victims,
                      "session_delete_by_filter %s (%d)" % (kind, len(victims)),
                      extra={"filters": filters, "sample": preview, "dry_run": False})


@handler("set_net")
def cmd_set_net(kicad, args):
    """Assign an EXISTING net to tracks/vias/zones/pads/shapes, in one commit."""
    ids = [str(i) for i in (args.get("item_ids") or [])]
    if not ids:
        fail("item_ids is required (a list of KIID strings)")
    net_name = args.get("net_name")
    if net_name is None:
        fail("net_name is required (use \"\" to clear the net)")
    board = need_board(kicad)
    layers = Layers(board)
    net = need_net(board, net_name)
    if net is None:
        net = Net(name="")

    items, missing = find_by_ids(board, ids)
    if not items:
        fail("none of the given item_ids exist on this board", missing=missing)
    changed = []
    skipped = []
    for item in items:
        if not hasattr(type(item), "net"):
            skipped.append({"id": item_id(item), "kind": kind_of(item),
                            "reason": "this item kind carries no net"})
            continue
        try:
            item.net = net
        except Exception as exc:
            skipped.append({"id": item_id(item), "kind": kind_of(item), "reason": _exc(exc)})
            continue
        changed.append(item)
    if not changed:
        fail("no item in the list can carry a net", skipped=skipped)

    with Commit(board, "session_set_net %s (%d)" % (net_name, len(changed)),
                enabled=len(changed) > 1) as commit:
        try:
            board.update_items(changed)
        except Exception as exc:
            commit.drop()
            fail("update_items failed: %s" % _exc(exc), commit=commit.state)
        commit.push()

    fresh = need_board(kicad)
    fresh_layers = Layers(fresh)
    got, gone = find_by_ids(fresh, [item_id(i) for i in changed])
    rows = []
    wrong = []
    for item in got:
        name = item_net_name(item)
        ok = (name or "") == (net_name or "")
        if not ok:
            wrong.append(item_id(item))
        rows.append({"id": item_id(item), "kind": kind_of(item), "net": name,
                     "applied": ok, "layer": (fresh_layers.label(item_layers(item)[0])
                                              if item_layers(item) else None)})
    payload = {"requested_net": net_name, "items": rows, "changed_count": len(rows)}
    if skipped:
        payload["skipped"] = skipped
    if missing:
        payload["missing_ids"] = missing
    if gone:
        payload["vanished_ids"] = gone
    if wrong:
        payload["unapplied_ids"] = wrong
    write_result(payload, applied=bool(rows) and not wrong and not gone, commit=commit)


@handler("set_enabled_layers")
def cmd_set_enabled_layers(kicad, args):
    """Set the board's enabled layer set. DESTRUCTIVE: content on removed layers is lost.

    The copper layer count is derived from the copper layers in the list (KiCad wants it
    separately and requires it even and >= 2); non-copper layers are passed through.
    """
    names = args.get("layer_names")
    if not isinstance(names, list) or not names:
        fail("layer_names must be a non-empty list of layer names")
    board = need_board(kicad)
    layers = Layers(board)
    before = layers.names()
    try:
        before_copper = board.get_copper_layer_count()
    except Exception:
        before_copper = None

    # Resolve against ALL known layer names, not just the enabled ones: this call is how a
    # layer gets enabled in the first place.
    resolved = []
    unknown = []
    for name in names:
        key = str(name).strip()
        idx = getattr(BoardLayer, "BL_" + key.replace(".", "_"), None)
        if idx is None:
            try:
                idx = board.get_layer_by_name(key)
            except Exception:
                idx = None
        # get_layer_by_name answers an unknown name with BL_UNDEFINED/BL_UNKNOWN rather than
        # an error, which would otherwise sail through as a real layer.
        if idx is None or canon_layer(int(idx)) in ("UNDEFINED", "UNKNOWN"):
            unknown.append(key)
        else:
            resolved.append(int(idx))
    if unknown:
        fail("unknown layer name(s): %s" % ", ".join(unknown),
             valid_layers=sorted(n[3:].replace("_", ".") for n in dir(BoardLayer)
                                 if n.startswith("BL_")))

    copper = [i for i in resolved if canon_layer(i).endswith(".Cu")]
    non_copper = [i for i in resolved if i not in copper]
    count = len(copper)
    if count < 2 or count % 2:
        fail("copper layer count must be even and >= 2, got %d" % count,
             copper_layers=[canon_layer(i) for i in copper],
             hint="include both F.Cu and B.Cu (and inner layers in pairs)")

    try:
        after_ids = board.set_enabled_layers(count, non_copper)
    except Exception as exc:
        fail("set_enabled_layers failed: %s" % _exc(exc), before=before,
             requested=[str(n) for n in names])

    fresh = need_board(kicad)
    fresh_layers = Layers(fresh)
    after = fresh_layers.names()
    want = {canon_layer(i) for i in resolved}
    have = set(after)
    payload = {"before": before, "after": after,
               "before_copper_layer_count": before_copper,
               "after_copper_layer_count": (fresh.get_copper_layer_count()
                                            if hasattr(fresh, "get_copper_layer_count")
                                            else None),
               "requested": sorted(want),
               "returned_layer_count": len(after_ids),
               "missing_requested": sorted(want - have),
               "extra_enabled": sorted(have - want)}
    write_result(payload, applied=not payload["missing_requested"],
                 note="content on layers that were removed is gone and cannot be undone "
                      "through this API; " + SAVE_NOTE)


@handler("set_origin")
def cmd_set_origin(kicad, args):
    """Move the grid origin (which='grid') or the drill/place origin (which='drill')."""
    which = str(args.get("which") or "grid").strip().lower()
    kinds = {"grid": board_commands_pb2.BoardOriginType.BOT_GRID,
             "drill": board_commands_pb2.BoardOriginType.BOT_DRILL,
             "place": board_commands_pb2.BoardOriginType.BOT_DRILL}
    if which not in kinds:
        fail("which must be grid|drill", valid=["grid", "drill"])
    if args.get("x_mm") is None or args.get("y_mm") is None:
        fail("x_mm and y_mm are required")
    x, y = float(args["x_mm"]), float(args["y_mm"])
    board = need_board(kicad)
    kind = kinds[which]
    try:
        before = xy_mm(board.get_origin(kind))
    except Exception as exc:
        fail("get_origin failed: %s" % _exc(exc))
    try:
        board.set_origin(kind, Vector2.from_xy_mm(x, y))
    except Exception as exc:
        fail("set_origin failed: %s" % _exc(exc), before=before)

    fresh = need_board(kicad)
    try:
        after = xy_mm(fresh.get_origin(kind))
    except Exception as exc:
        fail("wrote the origin but could not read it back: %s" % _exc(exc), before=before)
    applied = close(nm(after[0]), nm(x)) and close(nm(after[1]), nm(y))
    write_result({"which": which, "requested_mm": [x, y], "before_mm": before,
                  "after_mm": after}, applied=applied)


@handler("refill_zones")
def cmd_refill_zones(kicad, args):
    """Refill every zone (blocking), then report each zone's filled flag from a fresh read."""
    board = need_board(kicad)
    layers = Layers(board)
    try:
        before = [{"id": item_id(z), "name": z.name, "filled": bool(z.filled),
                   "layers": [layers.label(i) for i in item_layers(z)]}
                  for z in board.get_zones()]
    except Exception as exc:
        fail("get_zones failed: %s" % _exc(exc))
    if not before:
        emit({"ok": True, "zone_count": 0, "outputs": [], "mutated": [], "saved": False,
              "note": "the board has no zones; nothing to refill"})
    try:
        board.refill_zones(block=True)
    except Exception as exc:
        fail("refill_zones failed: %s" % _exc(exc), zone_count=len(before))

    fresh = need_board(kicad)
    fresh_layers = Layers(fresh)
    after = [{"id": item_id(z), "name": z.name, "filled": bool(z.filled),
              "layers": [fresh_layers.label(i) for i in item_layers(z)]}
             for z in fresh.get_zones()]
    filled = [z for z in after if z["filled"]]
    write_result({"zone_count": len(after), "filled_count": len(filled),
                  "unfilled_count": len(after) - len(filled),
                  "before": before, "after": after},
                 applied=bool(after),
                 note="zones that stay unfilled usually have no net, no copper island large "
                      "enough to keep, or sit outside the board outline; " + SAVE_NOTE)


@handler("run_action")
def cmd_run_action(kicad, args):
    """Run a named KiCad TOOL_ACTION in the session (e.g. 'pcbnew.Control.zoneFillAll').

    UNSTABLE by KiCad's own warning: action names are not API and are not guaranteed
    between versions.  The status is reported verbatim (RAS_OK / RAS_INVALID /
    RAS_FRAME_NOT_OPEN); an action that ran is NOT proof that it did anything.
    """
    action = str(args.get("action_name") or "").strip()
    if not action:
        fail("action_name is required")
    connect_board = get_board(kicad)[0]
    try:
        resp = kicad.run_action(action)
    except Exception as exc:
        headless_fail("run_action", exc, action=action)
        fail("run_action(%r) failed: %s" % (action, _exc(exc)))
    status = int(getattr(resp, "status", 0))
    names = {0: "RAS_UNKNOWN", 1: "RAS_OK", 2: "RAS_INVALID", 3: "RAS_FRAME_NOT_OPEN"}
    try:
        label = editor_commands_pb2.RunActionStatus.Name(status)
    except Exception:
        label = names.get(status, str(status))
    ok = label == "RAS_OK"
    payload = {"action": action, "status": label, "status_code": status,
               "board_open": connect_board is not None}
    if not ok:
        payload["error"] = "KiCad refused the action (%s)" % label
        if label == "RAS_INVALID":
            payload["hint"] = ("no TOOL_ACTION by that name in this build; action names are "
                               "unstable and undocumented")
    write_result(payload, applied=ok,
                 note="whether the action changed anything is not observable from its "
                      "status; re-read the board to confirm. " + SAVE_NOTE)


@handler("revert")
def cmd_revert(kicad, args):
    """Throw away every unsaved session change: reload the board from its file on disk."""
    board = need_board(kicad)
    layers = Layers(board)

    def counts(b):
        out = {}
        for kind in KINDS:
            try:
                out[kind] = len(items_of_kind(b, kind))
            except Exception:
                out[kind] = None
        return out

    before = counts(board)
    doc_name = board.document.board_filename
    try:
        board.revert()
    except Exception as exc:
        headless_fail("revert", exc, document=doc_name,
                      workaround="session_close() then session_open(<same path>) reloads the "
                                 "file from disk, which is what revert would have done")
        fail("revert failed: %s" % _exc(exc), document=doc_name)

    fresh, err = get_board(kicad)
    if fresh is None:
        fail("reverted but the session no longer exposes a board: %s" % err,
             document=doc_name)
    after = counts(fresh)
    changed = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
    write_result({"document": doc_name, "counts_before": before, "counts_after": after,
                  "counts_changed": changed,
                  "layer_count": len(Layers(fresh).names())},
                 applied=True,
                 note="the session now matches the file on disk; unsaved edits are gone "
                      "and cannot be recovered")
    del layers


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------


def main(argv):
    if len(argv) < 2:
        fail("usage: session_worker.py <subcommand> '<json payload>'",
             subcommands=sorted(HANDLERS))
    sub = argv[1]
    raw = argv[2] if len(argv) > 2 else "{}"
    handler_fn = HANDLERS.get(sub)
    if handler_fn is None:
        fail("unknown subcommand %r" % sub, subcommands=sorted(HANDLERS))
    try:
        args = json.loads(raw or "{}")
    except Exception as exc:
        fail("payload is not valid JSON: %s" % _exc(exc), payload_head=raw[:200])
    if not isinstance(args, dict):
        fail("payload must be a JSON object")
    kicad = connect()
    try:
        handler_fn(kicad, args)
    except SystemExit:
        raise
    except Exception as exc:
        fail("%s crashed: %s" % (sub, _exc(exc)))
    fail("%s produced no answer" % sub)


if __name__ == "__main__":
    main(sys.argv)
