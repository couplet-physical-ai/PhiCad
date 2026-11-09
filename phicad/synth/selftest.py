#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
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
"""Self-test for the synth pipeline: everything that does not need a build tree.

The stages that matter here are the ones whose failures are *quiet*.  A router that
refuses a net says so; a placer that packs two courtyards 0.02 mm apart, a netlist whose
component UUIDs drift between runs, or a validator that stops catching an unnetted pad all
look exactly like success until DRC or a fab house disagrees.  So the assertions are
mostly invariants rather than golden values:

  1.  library     pads and courtyards parse out of the real stock footprints
  2.  ir          each class of design hole is caught, and a good design passes
  3.  netlist     deterministic, and its connectivity round-trips the design
  4.  place       no courtyard overlaps, channels fit a track and a via, deterministic
  5.  route       every connection made on a design that has room, and refused (not
                  faked) on one that does not
  6.  emit        splicing is idempotent, and strip removes exactly what it added
  7.  silk        every label clears the pads it is supposed to clear

Stdlib only, no build tree, no network, no IPC — safe for CI.  The parts that need the
fork's kicad-cli (import, DRC) are covered by `cli.py build` and are deliberately not here:
this file has to stay runnable on a checkout with nothing built.

Exit 0 = pass.

    python3 phicad/synth/selftest.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import emit  # noqa: E402
import ir  # noqa: E402
import netlist as netlist_mod  # noqa: E402
import place as place_mod  # noqa: E402
import project as project_mod  # noqa: E402
import route as route_mod  # noqa: E402
import schematic as schematic_mod  # noqa: E402
import silk as silk_mod  # noqa: E402
from library import Library, LibraryError, rotate_point  # noqa: E402
from symbols import SymbolError, SymbolLibrary  # noqa: E402

R0805 = "Resistor_SMD:R_0805_2012Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
LGA8 = "Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering"
HDR4 = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"

# A design with room to route: two passives and a header, nothing fine-pitch.
EASY = {
    "project": "selftest-easy",
    "board": {"layers": 2, "outline": "auto", "origin": [100, 100]},
    "parts": {"J1": HDR4, "R1": R0805, "C1": C0805},
    "nets": {
        "GND": ["J1.1", "C1.2"],
        "3V3": ["J1.2", "R1.2", "C1.1"],
        "SDA": ["J1.3", "R1.1"],
    },
    "no_connect": ["J1.4"],
}


def design_from(payload: dict) -> ir.Design:
    return ir.from_dict(json.loads(json.dumps(payload)))


def check_library(lib: Library) -> None:
    fp = lib.resolve(R0805)
    assert len(fp.pads) == 2, fp.pads
    # Values from the stock file; the demo generator's own comment cites +/-0.9125.
    assert abs(fp.pads["1"].x + 0.9125) < 1e-9, fp.pads["1"]
    assert abs(fp.pads["2"].x - 0.9125) < 1e-9, fp.pads["2"]
    assert fp.courtyard is not None and fp.courtyard[0] < 0 < fp.courtyard[2]
    assert fp.smd

    # A 90 degree rotation swaps a pad's extents and maps local -x to +y.
    pad = fp.pads["1"]
    w0, h0 = pad.extent(0)
    w90, h90 = pad.extent(90)
    assert abs(w0 - h90) < 1e-9 and abs(h0 - w90) < 1e-9, (pad.extent(0), pad.extent(90))
    rx, ry = rotate_point(-1.0, 0.0, 90)
    assert abs(rx) < 1e-9 and abs(ry - 1.0) < 1e-9, (rx, ry)

    header = lib.resolve(HDR4)
    assert len(header.pads) == 4, header.pads
    assert all(p.through for p in header.pads.values()), "header pads should be thru-hole"

    try:
        lib.resolve("Resistor_SMD:R_0805_definitely_not_real")
    except LibraryError as exc:
        assert "not found" in str(exc)
    else:                                                          # pragma: no cover
        raise AssertionError("a missing footprint should raise")

    # The body must not carry the library's own Reference/Value, or the board inherits a
    # stray REF** on the silkscreen and DRC reports it.
    assert '(property "Reference"' not in fp.body
    assert '(property "Value"' not in fp.body
    assert "(pad " in fp.body

    # Module footprints exercise two constructs that ordinary passives do not.  The ESP32's
    # subdivided thermal-pad apertures live only on F.Paste and are not electrical pins, while
    # its antenna carries a tracks-not-allowed zone that has to survive parsing for the router.
    module = lib.resolve("RF_Module:ESP32-C3-WROOM-02")
    assert len([pad for pad in module.pads.values() if pad.plated]) == 19, module.pads
    assert "" in module.pads and not module.pads[""].plated, module.pads.get("")
    assert module.keepouts == ((-14.0, -18.1, 14.0, -7.1),), module.keepouts
    print("ok 1 - library: dimensions, body scrubbing, module paste and keepouts")


def check_schematic_edit_guard(lib: Library) -> None:
    """A hand-edited schematic must survive the next build.

    The emitter is deterministic, so regeneration is byte-identical -- which also means an
    unguarded rebuild silently destroys every edit a human made. The stamp in the title
    block is what tells "nobody touched this" apart from "somebody moved a symbol".
    """
    sch = schematic_mod

    design = design_from(EASY)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        path = sch.write(design, out, library=lib)
        first = path.read_text(encoding="utf-8")
        assert sch._stamp_of(first), "generated schematic carries no stamp"
        assert sch._stamp_matches(first), "a freshly generated schematic fails its own stamp"

        # Regenerating an untouched file is fine and stays byte-identical.
        sch.write(design, out, library=lib)
        assert path.read_text(encoding="utf-8") == first, "regeneration is not deterministic"

        # Now edit it the way a human would: move the first placed symbol. Find a real
        # coordinate in this sheet rather than assuming one, so the test does not depend
        # on the layout algorithm's output.
        m = re.search(r'\(symbol\s+\(lib_id "[^"]+"\)\s*\(at ([\d.]+) ', first)
        assert m, "no placed symbol found in the generated schematic"
        edited = first.replace(f"(at {m.group(1)} ", "(at 160.02 ", 1)
        assert edited != first, "test could not find a symbol position to move"
        path.write_text(edited, encoding="utf-8")
        assert not sch._stamp_matches(path.read_text(encoding="utf-8"))

        try:
            sch.write(design, out, library=lib)
        except sch.SchematicError as exc:
            assert "edited" in str(exc), str(exc)
        else:
            raise AssertionError("a hand-edited schematic was silently overwritten")
        assert path.read_text(encoding="utf-8") == edited, "the edit was clobbered anyway"

        # The escape hatch still works, on purpose.
        sch.write(design, out, library=lib, overwrite_edits=True)
        assert sch._stamp_matches(path.read_text(encoding="utf-8"))


def check_ir(lib: Library) -> None:
    good = design_from(EASY)
    assert not ir.errors(good.validate(lib)), ir.format_problems(good.validate(lib))

    def holes(mutate) -> list:
        payload = json.loads(json.dumps(EASY))
        mutate(payload)
        return good.validate(lib) if payload is None else \
            design_from(payload).validate(lib)

    def where(problems, fragment: str) -> bool:
        return any(fragment in f"{p.where}: {p.message}" for p in ir.errors(problems))

    # An unnetted pad is the connectivity gate's whole reason to exist.
    p = holes(lambda d: d["nets"]["GND"].remove("C1.2"))
    assert where(p, "pad 2 is in no net"), ir.format_problems(p)

    # A pad number the footprint does not have.
    p = holes(lambda d: d["nets"]["SDA"].append("R1.9"))
    assert where(p, "has no pad '9'"), ir.format_problems(p)

    # The same pad on two nets is a short.
    p = holes(lambda d: d["nets"]["GND"].append("J1.2"))
    assert where(p, "already on net"), ir.format_problems(p)

    # An unresolvable footprint.
    p = holes(lambda d: d["parts"].update({"U9": "Nope_Lib:Nope_Part"}))
    assert where(p, "is not in any library"), ir.format_problems(p)

    # A net naming a part that does not exist.
    p = holes(lambda d: d["nets"].update({"X": ["Q7.1", "J1.3"]}))
    assert where(p, "no part 'Q7'"), ir.format_problems(p)

    # A pin both netted and declared no-connect cannot be both.
    p = holes(lambda d: d["no_connect"].append("J1.3"))
    assert where(p, "declared no-connect"), ir.format_problems(p)

    # A placement constraint written inside a part is a mistake, not a schematic field.
    # This used to fall through to `fields` and become a symbol field named "edge": the
    # placer never saw it, so a design carrying `"edge": "left"` -- not even a compass
    # direction -- validated clean. It has to be rejected at parse time, because by the
    # time validate() runs the key is indistinguishable from a deliberate field.
    for key, value in (("edge", "left"), ("near", "U1"), ("group", "power")):
        payload = json.loads(json.dumps(EASY))
        # EASY's parts use the bare-footprint shorthand, so expand one to object form.
        payload["parts"]["R1"] = {"footprint": payload["parts"]["R1"], key: value}
        try:
            design_from(payload)
        except ir.IRError as exc:
            assert "placement" in str(exc), str(exc)
            assert key in str(exc), str(exc)
        else:
            raise AssertionError(f"parts.R1.{key} was accepted as a field")

    # A one-pin net connects nothing: a warning, not an error.
    p = holes(lambda d: d["nets"].update({"SPARE": ["J1.4"]}))
    warnings = [x for x in p if x.severity == ir.SEVERITY_WARNING]
    assert any("only one pin" in w.message for w in warnings), ir.format_problems(p)

    # A design with no parts (or none connected) is unbuildable, so it must not validate:
    # `ok: true` is what lets the repair loop offer a design for human approval, and a
    # truncated or placeholder model reply must never qualify.
    empty = design_from({"project": "empty"})
    assert where(empty.validate(lib), "the design has no parts"), \
        ir.format_problems(empty.validate(lib))
    unconnected = design_from({"project": "unconnected",
                               "parts": dict(list(EASY["parts"].items())[:1])})
    assert where(unconnected.validate(lib), "the design has no nets"), \
        ir.format_problems(unconnected.validate(lib))

    # Boolean flags are a structural contract, not values to coerce with Python truthiness.
    # In particular, bool("false") is True: accepting it silently enables the ground pour,
    # changes routing, or locks a placement instead of reporting the malformed field.
    valid_flags = json.loads(json.dumps(EASY))
    valid_flags["board"]["pour_ground"] = False
    valid_flags["placement"] = {"R1": {"lock": False}}
    valid_flags["parts"]["R1"] = {
        "footprint": R0805, "dnp": False, "exclude_from_bom": False,
    }
    loaded = design_from(valid_flags)
    assert loaded.board.pour_ground is False
    assert loaded.placement["R1"].lock is False
    assert loaded.parts["R1"].dnp is False
    assert loaded.parts["R1"].exclude_from_bom is False

    malformed_flags = [
        (lambda d: d["board"].update({"pour_ground": "false"}),
         "board.pour_ground: expected true or false, got 'false'"),
        (lambda d: d.setdefault("placement", {}).update({"R1": {"lock": 0}}),
         "placement.R1.lock: expected true or false, got 0"),
        (lambda d: d["parts"].update({"R1": {"footprint": R0805, "dnp": "true"}}),
         "parts.R1.dnp: expected true or false, got 'true'"),
        (lambda d: d["parts"].update({
            "R1": {"footprint": R0805, "exclude_from_bom": 1}}),
         "parts.R1.exclude_from_bom: expected true or false, got 1"),
    ]
    for mutate, expected in malformed_flags:
        payload = json.loads(json.dumps(EASY))
        mutate(payload)
        try:
            design_from(payload)
        except ir.IRError as exc:
            assert str(exc) == expected, str(exc)
        else:                                                       # pragma: no cover
            raise AssertionError(f"malformed boolean was accepted: {expected}")

    print("ok 2 - ir: design holes and malformed booleans caught, a good design passes")


def check_netlist(lib: Library) -> None:
    import sexp_edit

    design = design_from(EASY)
    first = netlist_mod.emit(design, when="2026-01-01")
    second = netlist_mod.emit(design, when="2026-01-01")
    assert first == second, "netlist emission is not deterministic"

    uid_a = netlist_mod.component_uuid("selftest-easy", "R1")
    uid_b = netlist_mod.component_uuid("selftest-easy", "R1")
    assert uid_a == uid_b and uid_a in first
    assert netlist_mod.component_uuid("other", "R1") != uid_a, \
        "component ids must be scoped to the project"

    # Parse the emitted text, rather than comparing the design's connectivity to itself.
    # The parser rejects truncated s-expressions, so this is the regression guard for the
    # root node's closing parenthesis as well as for the structure below it.
    assert first.count("(") == first.count(")"), "netlist parentheses are unbalanced"
    parsed = sexp_edit.parse(first)
    doc = parsed[0] if isinstance(parsed, list) else parsed
    assert doc.head == "export", doc.head

    components = doc.child("components")
    assert components is not None
    parsed_components = components.children("comp")
    assert len(parsed_components) == len(design.parts), parsed_components
    parsed_refs = {comp.scalar("ref") for comp in parsed_components}
    assert parsed_refs == set(design.parts), parsed_refs

    conn = netlist_mod.connectivity(design)
    nets = doc.child("nets")
    assert nets is not None
    parsed_nets = nets.children("net")
    assert len(parsed_nets) == len(conn), parsed_nets
    parsed_conn = {}
    parsed_node_count = 0
    for net in parsed_nets:
        name = net.scalar("name")
        nodes = net.children("node")
        assert name is not None and name not in parsed_conn, name
        parsed_conn[name] = sorted(
            f"{node.scalar('ref')}.{node.scalar('pin')}" for node in nodes)
        parsed_node_count += len(nodes)
    assert parsed_node_count == sum(len(pins) for pins in conn.values()), parsed_conn
    assert parsed_conn == conn, (conn, parsed_conn)

    # A no-connect pad still gets a net of its own: left out entirely the updater warns,
    # and a real omission would then hide among the intentional ones.
    assert netlist_mod.unconnected_net_name("J1", "4") in parsed_conn

    assert conn["GND"] == ["C1.2", "J1.1"], conn["GND"]
    assert netlist_mod.compare_connectivity(conn, conn) == []
    broken = dict(conn)
    broken["3V3"] = [p for p in broken["3V3"] if p != "C1.1"]
    diffs = netlist_mod.compare_connectivity(conn, broken)
    assert diffs and "C1.1" in diffs[0], diffs
    print("ok 3 - netlist: deterministic, parsed export, connectivity round-trips")


def check_place(lib: Library) -> None:
    design = design_from(EASY)
    first = place_mod.solve(design, lib)
    second = place_mod.solve(design, lib)
    assert first.positions() == second.positions(), "placement is not deterministic"

    assert first.overlaps(0.0) == [], first.overlaps(0.0)
    assert first.outline is not None

    # The channel between parts has to fit the widest track AND, on a poured board, a
    # stitching via -- otherwise the placement is courtyard-legal and unroutable.
    gap = place_mod.routing_gap(design)
    rules = design.rules
    assert gap >= 2 * rules.clearance + rules.track_width, gap
    assert gap >= rules.via_diameter_min + 2 * rules.clearance, gap
    assert first.overlaps(gap * 0.99) == [], "parts are closer than the routing gap"

    # The outline is shrink-wrapped: every courtyard inside it, and the margin honoured.
    margin = max(design.board.margin, rules.edge_clearance)
    for ref, placed in first.parts.items():
        box = placed.box()
        assert box[0] >= first.outline.left + margin - 1e-6, ref
        assert box[2] <= first.outline.right - margin + 1e-6, ref

    # An explicit position is a statement about the world and must survive legalization.
    payload = json.loads(json.dumps(EASY))
    payload["placement"] = {"J1": {"at": [120.0, 130.0], "rot": 90}}
    pinned = place_mod.solve(design_from(payload), lib)
    assert abs(pinned.parts["J1"].x - 120.0) < 1e-6, pinned.parts["J1"]
    assert abs(pinned.parts["J1"].rot - 90.0) < 1e-6, pinned.parts["J1"]
    print("ok 4 - place: deterministic, legal, channels fit a via, pins are honoured")


def check_route(lib: Library) -> None:
    design = design_from(EASY)
    layout = place_mod.solve(design, lib)
    result = route_mod.route(design, layout, lib)
    assert not result.failures, [str(f) for f in result.failures]
    assert result.tracks, "a routed board should have copper"

    # Every net that needed routing has copper, and every ground pad reaches the pour.
    routed_nets = {t.net for t in result.tracks} | {v.net for v in result.vias}
    for net in ("3V3", "SDA"):
        assert net in routed_nets, (net, routed_nets)
    ground_pads = {s.pin for s in route_mod.pad_sites(design, layout, lib)
                   if s.net == design.board.ground_net}
    assert ground_pads <= set(result.stitched), ground_pads - set(result.stitched)

    # No two vias of the same net on top of each other.
    for i, a in enumerate(result.vias):
        for b in result.vias[i + 1:]:
            if a.net != b.net:
                continue
            distance = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
            assert distance > 0.3, (a, b, distance)

    # Same-net drills too close for the project's 0.25 mm rule become one via, with a bridge
    # on both layers so the discarded centre's tracks stay connected.  A tangent track gets a
    # full-width bridge too; merely deleting the second via was the old, unsafe temptation.
    contact_vias = [route_mod.Via(0.0, 0.0, "VCC", 0.8, 0.4),
                    route_mod.Via(0.1, 0.0, "VCC", 0.8, 0.4)]
    contact_tracks = [route_mod.Track(0.1, 0.0, 0.1, 1.0, "VCC", "F.Cu", 0.4),
                      route_mod.Track(0.6, -1.0, 0.6, 1.0, "VCC", "B.Cu", 0.4)]
    repaired_vias, repair_tracks = route_mod._repair_same_net_contacts(
        contact_vias, contact_tracks, ("F.Cu", "B.Cu"), design.rules)
    assert len(repaired_vias) == 1, repaired_vias
    repair_layers = {track.layer for track in repair_tracks
                     if abs(track.x2 - track.x1) <= 0.1 + 1.0e-9}
    assert repair_layers == {"F.Cu", "B.Cu"}, repair_tracks
    assert any(abs(track.x2 - track.x1) >= 0.6 - 1.0e-9
               for track in repair_tracks), repair_tracks

    # A design with no room must be REFUSED, not faked: two parts pinned on top of one
    # another leave no legal path, and a router that returned copper anyway would be
    # producing violations for DRC to find later.
    payload = json.loads(json.dumps(EASY))
    payload["board"]["part_gap"] = 0.05
    payload["placement"] = {"R1": {"at": [110.0, 110.0], "lock": True},
                            "C1": {"at": [110.35, 110.0], "lock": True},
                            "J1": {"at": [125.0, 125.0], "lock": True}}
    tight = design_from(payload)
    tight_layout = place_mod.solve(tight, lib)
    tight_result = route_mod.route(tight, tight_layout, lib)
    assert tight_result.failures, "an impossible board should be refused, not routed"
    assert all(f.reason for f in tight_result.failures), "a refusal must say why"

    # Force a net from the ESP32's centre pad to a connector on the far side of its antenna.
    # Without the footprint keepout the shortest route is a vertical line through the antenna;
    # with the keepout it must make a visible detour around x=6 mm or x=34 mm.
    module_payload = {
        "project": "module-keepout-test",
        "board": {"layers": 2, "outline": "rect", "width": 40, "height": 45,
                  "pour_ground": False, "margin": 1},
        "parts": {
            "U1": {"footprint": "RF_Module:ESP32-C3-WROOM-02",
                   "symbol": "RF_Module:ESP32-C3-WROOM-02", "value": "ESP32-C3"},
            "J1": {"footprint":
                   "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical",
                   "symbol": "Connector_Generic:Conn_01x01", "value": "test"},
        },
        "nets": {"SIG": ["U1.19", "J1.1"]},
        "no_connect": ["U1.{}".format(pin) for pin in range(1, 19)],
        "placement": {"U1": {"at": [20, 30], "lock": True},
                      "J1": {"at": [20, 4], "lock": True}},
    }
    module_design = design_from(module_payload)
    module_problems = module_design.validate(lib)
    assert not ir.errors(module_problems), ir.format_problems(module_problems)
    module_layout = place_mod.solve(module_design, lib)
    module_result = route_mod.route(module_design, module_layout, lib)
    assert not module_result.failures, [str(f) for f in module_result.failures]
    kx, ky, khw, khh = route_mod.keepout_sites(module_design, module_layout, lib)[0]
    for track in module_result.tracks:
        bounds_overlap = (
            max(track.x1, track.x2) >= kx - khw
            and min(track.x1, track.x2) <= kx + khw
            and max(track.y1, track.y2) >= ky - khh
            and min(track.y1, track.y2) <= ky + khh
        )
        assert not bounds_overlap, (track, (kx, ky, khw, khh))

    items = route_mod.copper_items(result)
    assert len(items) == len(result.tracks) + len(result.vias)
    assert all('(net "' in item for item in items), "the fork references nets by name"
    print("ok 5 - route: connects what it can, refuses what it cannot, no stacked vias")


def check_emit(lib: Library) -> None:
    design = design_from(EASY)
    layout = place_mod.solve(design, lib)
    with tempfile.TemporaryDirectory(prefix="phicad-synth-selftest-") as tmp:
        out = Path(tmp)
        written = project_mod.scaffold(design, out)
        board = written["board"]
        original = board.read_text(encoding="utf-8")

        items = emit.outline_items(layout.outline)
        added = emit.splice(board, items)
        assert added == len(items), (added, len(items))

        # Splicing the same items again must add nothing: a duplicated outline is still a
        # closed outline, so DRC would never report it.
        assert emit.splice(board, items) == 0, "splice is not idempotent"

        removed = emit.strip_generated(board)
        assert removed >= len(items), (removed, len(items))
        # Stripping must leave the board parseable and no larger than it started.
        after = board.read_text(encoding="utf-8")
        assert after.rstrip().endswith(")"), "strip broke the closing paren"
        assert len(after) <= len(original) + 2, "strip left generated items behind"

        pro = json.loads(written["project"].read_text(encoding="utf-8"))
        rules = pro["board"]["design_settings"]["rules"]
        assert rules["min_track_width"] == design.rules.min_track_width
        assert pro["meta"]["version"] == 3
        # The layer stack is physical order, not numeric: B.Cu is 2 while In1.Cu is 4.
        stack = project_mod.copper_layers(4)
        assert [name for _n, name in stack] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], stack
    print("ok 6 - emit: idempotent splice, clean strip, rules reach the project file")


def check_silk(lib: Library) -> None:
    design = design_from(EASY)
    layout = place_mod.solve(design, lib)
    labels = silk_mod.solve(design, layout, lib, layout.outline)
    assert set(labels) == set(design.parts), (sorted(labels), sorted(design.parts))

    again = silk_mod.solve(design, layout, lib, layout.outline)
    for ref, label in labels.items():
        assert (abs(label.x - again[ref].x) < 1e-9
                and abs(label.y - again[ref].y) < 1e-9), f"{ref} is not deterministic"

    for ref, label in sorted(labels.items()):
        assert label.size > 0 and label.layer.endswith("SilkS"), label
        # Labels stay horizontal: (at x y rot) takes its angle in the board frame, so a
        # non-zero angle here would rotate with the part rather than staying readable.
        assert abs(label.rot) < 1e-9, (ref, label.rot)
        assert label.clean, f"{ref} has no clear slot on a board with this much room"
    print("ok 7 - silk: every part labelled, deterministic, horizontal, clear")


def check_symbols() -> None:
    syms = SymbolLibrary.default()

    r = syms.resolve("Device:R")
    assert sorted(r.pins) == ["1", "2"], r.pins
    assert r.prefix == "R"
    assert all(p.etype == "passive" for p in r.pins.values())
    # The embedded body's ONLY edit is the outer name; unit sub-symbols keep bare names.
    emb = r.embedded()
    assert emb.lstrip().startswith('(symbol "Device:R"'), emb[:60]
    assert '(symbol "R_0_1"' in emb and '(symbol "R_1_1"' in emb

    # Flattening a derived symbol is the check that would have caught the silent-pin-loss
    # failure: embedded unflattened, a derived symbol contributes zero netlist nodes.
    ldo = syms.resolve("Regulator_Linear:AP2112K-3.3")
    assert ldo.extends, "AP2112K-3.3 should be a derived symbol"
    parent = syms.resolve(f"Regulator_Linear:{ldo.extends}")
    assert sorted(ldo.pins) == sorted(parent.pins) and len(ldo.pins) == 5, ldo.pins
    assert "(pin " in ldo.body, "a flattened body must carry the parent's pins"
    assert "(extends" not in ldo.body
    # The derived symbol's own identity survives the flatten: its value, its unit names.
    assert '(symbol "AP2112K-3.3_' in ldo.body
    assert '(symbol "AP2204K-1.5_' not in ldo.body

    bme = syms.resolve("Sensor:BME280")
    assert bme.power_in_pins() == ["1", "6", "7", "8"], bme.power_in_pins()

    try:
        syms.resolve("Device:R_definitely_not_real")
    except SymbolError:
        pass
    else:                                                          # pragma: no cover
        raise AssertionError("a missing symbol should raise")
    assert "Sensor:BME280" in syms.suggest("BME2800"), syms.suggest("BME2800")
    print("ok 8 - symbols: pins, derived flattening, embedding, suggestions")


def check_ir_symbols(lib: Library) -> None:
    syms = SymbolLibrary.default()

    good = design_from(EASY)
    assert not ir.errors(good.validate(lib, syms))

    # A package-generic footprint cannot imply a symbol; the design must declare one.
    payload = json.loads(json.dumps(EASY))
    payload["parts"]["U1"] = {"footprint": "Package_TO_SOT_SMD:SOT-23-5"}
    payload["nets"]["GND"] += ["U1.1", "U1.2", "U1.3", "U1.4", "U1.5"]
    p = design_from(payload).validate(lib, syms)
    assert any("no symbol" in q.message for q in ir.errors(p)), ir.format_problems(p)

    # A symbol whose pins do not match the footprint's pads is a circuit-level error
    # nothing downstream reports: the sheet and the board would disagree about U1.5.
    payload["parts"]["U1"]["symbol"] = "Device:R"
    p = design_from(payload).validate(lib, syms)
    assert any("disagree" in q.message for q in ir.errors(p)), ir.format_problems(p)

    # And the defaults: a pin header maps to a Conn_01xNN sized from its pad count.
    assert ir.default_symbol(HDR4, 4) == "Connector_Generic:Conn_01x04"
    assert ir.default_symbol(R0805, 2) == "Device:R"
    assert ir.default_symbol("Package_TO_SOT_SMD:SOT-23-5", 5) == ""
    print("ok 9 - ir: symbol defaults, undeclarable packages, pin/pad mismatch")


# A board with everything `board.py` has to get right and a generated board never shows it:
# a rotated footprint whose pads carry no angle, a hidden reference, footprint Value text on
# the silkscreen rather than F.Fab, board-level silk, and a non-default
# `pad_to_mask_clearance`.  Written out longhand rather than generated, because the point of
# the fixture is to be the shapes an *existing* board has.
def _fixture_footprint(ref: str, x: float, y: float, rot: float, hide: str = "") -> str:
    tag = ref.lower()
    return f'''	(footprint "sel:RES"
		(layer "F.Cu")
		(at {x} {y}{f" {rot}" if rot else ""})
		(property "Reference" "{ref}"
			(at 0 -2 0){hide}
			(layer "F.SilkS")
			(uuid "{tag}00000-0000-0000-0000-000000000001")
			(effects (font (size 1 1) (thickness 0.15))))
		(property "Value" "1k"
			(at 0 2 0)
			(layer "F.SilkS")
			(uuid "{tag}00000-0000-0000-0000-000000000002")
			(effects (font (size 1 1) (thickness 0.15))))
		(fp_line (start -1.6 -0.9) (end 1.6 -0.9)
			(stroke (width 0.05) (type solid)) (layer "F.CrtYd")
			(uuid "{tag}00000-0000-0000-0000-000000000003"))
		(fp_line (start -1.6 0.9) (end 1.6 0.9)
			(stroke (width 0.05) (type solid)) (layer "F.CrtYd")
			(uuid "{tag}00000-0000-0000-0000-000000000004"))
		(fp_line (start -1.6 -0.9) (end -1.6 0.9)
			(stroke (width 0.05) (type solid)) (layer "F.CrtYd")
			(uuid "{tag}00000-0000-0000-0000-000000000005"))
		(fp_line (start 1.6 -0.9) (end 1.6 0.9)
			(stroke (width 0.05) (type solid)) (layer "F.CrtYd")
			(uuid "{tag}00000-0000-0000-0000-000000000006"))
		(pad "1" smd rect (at -0.9 0) (size 1 1.45)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(uuid "{tag}00000-0000-0000-0000-000000000007"))
		(pad "2" smd rect (at 0.9 0) (size 1 1.45)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(uuid "{tag}00000-0000-0000-0000-000000000008"))
	)'''


FIXTURE_BOARD = '''(kicad_pcb
	(version 20260206)
	(generator "pcbnew")
	(generator_version "10.99")
	(paper "A4")
	(layers (0 "F.Cu" signal) (2 "B.Cu" signal))
	(setup
		(pad_to_mask_clearance 0.03)
	)
	(net 0 "")
	(gr_rect (start 0 0) (end 40 30)
		(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts")
		(uuid "ffffffff-0000-0000-0000-000000000001"))
	(gr_text "BOARD NOTE"
		(at 20 27 0)
		(layer "F.SilkS")
		(uuid "ffffffff-0000-0000-0000-000000000002")
		(effects (font (size 1 1) (thickness 0.15))))
	(gr_line (start 2 26) (end 8 26)
		(stroke (width 0.15) (type solid)) (layer "F.SilkS")
		(uuid "ffffffff-0000-0000-0000-000000000003"))
''' + "\n".join([
    _fixture_footprint("R1", 10, 10, 0),
    _fixture_footprint("R2", 25, 10, 90),
    _fixture_footprint("R3", 10, 20, 0, hide="\n\t\t\t(hide yes)"),
]) + "\n)\n"


def check_board() -> None:
    import board as board_mod

    with tempfile.TemporaryDirectory(prefix="phicad-synth-selftest-") as tmp:
        path = Path(tmp) / "fixture.kicad_pcb"
        path.write_text(FIXTURE_BOARD, encoding="utf-8")
        model = board_mod.read(path)

        # --- what the board says, not what a generated one would ------------------------
        assert abs(model.mask_expansion - 0.03) < 1e-9, model.mask_expansion
        assert (model.outline.left, model.outline.top,
                model.outline.right, model.outline.bottom) == (0.0, 0.0, 40.0, 30.0)
        assert not model.notes, model.notes

        # A hidden reference is not plotted, so it is neither solvable nor an obstacle.
        assert model.hidden == ("R3",), model.hidden
        assert sorted(model.layout.parts) == ["R1", "R2"], sorted(model.layout.parts)

        # --- pad angles are board-absolute ----------------------------------------------
        # R2 is rotated 90 and its pads carry no angle of their own, so on the board their
        # 1.45 mm dimension stays on Y.  `Pad.extent(fp_rot)` composing the two would swap
        # it, which is right for instantiating a .kicad_mod and wrong for reading a board.
        r2 = model.library.resolve(model.design.parts["R2"].footprint)
        assert r2.pads["1"].extent(90.0) == (1.0, 1.45), r2.pads["1"].extent(90.0)
        r1 = model.library.resolve(model.design.parts["R1"].footprint)
        assert r1.pads["1"].extent(0.0) == (1.0, 1.45), r1.pads["1"].extent(0.0)
        # Positions DO rotate: local (-0.9, 0) at 90 degrees lands 0.9 mm further down.
        px, py = rotate_point(r2.pads["1"].x, r2.pads["1"].y, 90.0)
        placed = model.layout.parts["R2"]
        assert abs(placed.x + px - 25.0) < 1e-9 and abs(placed.y + py - 10.9) < 1e-9

        # --- immovable silkscreen becomes an obstacle, the references do not -------------
        # Three Value fields (R3's counts; only its *reference* is hidden), the board text
        # and the board line.  A reference field in here would make every part refuse the
        # position it already holds.
        assert len(model.fixed_silk["F"]) == 5, model.fixed_silk["F"]
        assert not model.fixed_silk["B"], model.fixed_silk["B"]
        note = [b for b in model.fixed_silk["F"] if b[0] < 20.0 < b[2] and b[1] < 27.0 < b[3]]
        assert len(note) == 1, "the board's own silk text is not an obstacle"

        # --- the solve ------------------------------------------------------------------
        labels = model.solve_silk()
        assert sorted(labels) == ["R1", "R2"], sorted(labels)
        again = board_mod.read(path).solve_silk()
        for ref, label in labels.items():
            assert (abs(label.x - again[ref].x) < 1e-9
                    and abs(label.y - again[ref].y) < 1e-9), f"{ref} is not deterministic"
            assert label.clean, f"{ref} has no clear slot on a board with this much room"
            assert label.layer == "F.SilkS", label

        # --- writing back moves the label and NOTHING else -------------------------------
        # The regression this file exists for: `set_references()` used to resolve the
        # reference property to its *enclosing* node, so the `(at …)` it rewrote was the
        # footprint's own position and every part on the board moved.
        before = {ref: (p.x, p.y, p.rot) for ref, p in model.layout.parts.items()}
        assert emit.set_references(path, labels) == 2

        after_model = board_mod.read(path)
        after = {ref: (p.x, p.y, p.rot) for ref, p in after_model.layout.parts.items()}
        assert after == before, f"set_references moved a footprint: {before} -> {after}"

        text = path.read_text(encoding="utf-8")
        assert "(at 25 10 90)" in text, "R2's footprint transform was rewritten"
        for ref, label in labels.items():
            assert f"(at {label.x:.4f} {label.y:.4f} {label.rot:g})" in text, ref
        # R3's reference is hidden, so it was not in `labels` and must be untouched.
        assert "(at 0 -2 0)" in text, "the hidden reference was rewritten"

    # --- the split a caller needs to read the residue correctly --------------------------
    fixable = {"type": "silk_overlap",
               "items": [{"description": "Reference field of R1"},
                         {"description": "Segment of R2 on F.Silkscreen"}]}
    stuck = {"type": "silk_overlap",
             "items": [{"description": "Segment of U1 on F.Silkscreen"},
                       {"description": "Segment of Y1 on F.Silkscreen"}]}
    other = {"type": "clearance", "items": [{"description": "Reference field of R1"}]}
    assert board_mod.unfixable([fixable, stuck, other]) == [stuck]
    print("ok 11 - board: instances read back, pad angles absolute, references rewritten alone")


def check_schematic(lib: Library) -> None:
    import sexp

    syms = SymbolLibrary.default()
    design = design_from(EASY)
    footprints = {ref: lib.resolve(part.footprint)
                  for ref, part in design.parts.items()}
    sheet = schematic_mod.layout(design, footprints, syms)
    text = schematic_mod.emit(design, sheet, syms)
    assert text == schematic_mod.emit(design, sheet, syms), "emit must be deterministic"

    doc = sexp.parse(text)
    assert sexp.scalar(doc, "version") == schematic_mod.SCH_VERSION

    # Every symbol origin on the 1.27 mm connection grid, or every pin costs an
    # endpoint_off_grid warning.
    for node in sexp.children(doc, "symbol"):
        at = sexp.floats(node, "at")
        assert at and all(abs(v / 1.27 - round(v / 1.27)) < 1e-6 for v in at[:2]), at

    # One global label per netted pin, anchored exactly on the pin's connection point —
    # coincidence with a PIN_END is what makes the label connect instead of dangle.
    labels = {}
    for node in sexp.children(doc, "global_label"):
        at = sexp.floats(node, "at")
        labels.setdefault(str(sexp.atoms(node)[0]), []).append((at[0], at[1]))
    owner = design.pin_owner()
    for pin, net in owner.items():
        ref, _, number = pin.partition(".")
        placed = sheet.placed[ref]
        px, py = placed.pin_at(number)
        assert any(abs(x - px) < 1e-6 and abs(y - py) < 1e-6
                   for x, y in labels.get(net, [])), (pin, net)

    # The declared no-connect pad gets a marker, not a label.
    ncs = sexp.children(doc, "no_connect")
    assert len(ncs) == len(design.no_connect) == 1
    nx, ny = sexp.floats(ncs[0], "at")[:2]
    px, py = sheet.placed["J1"].pin_at("4")
    assert abs(nx - px) < 1e-6 and abs(ny - py) < 1e-6

    # Embedded lib_symbols carry the real bodies (pins included) under Lib:Name.
    lib_symbols = sexp.child(doc, "lib_symbols")
    names = {str(sexp.atoms(n)[0]) for n in sexp.children(lib_symbols, "symbol")}
    assert {"Device:R", "Device:C", "Connector_Generic:Conn_01x04"} <= names, names
    print("ok 10 - schematic: grid, label-per-pin, no-connects, embedded symbols")


def check_project_history_hook() -> None:
    """`phicad synth build` must be able to give the project a Git history scope.

    The CLI used not to initialise history at all -- only the MCP `synth_build` tool did --
    so a project built from the terminal showed "Git not initialized" in Agent View and its
    Snapshot button had nothing to commit to, purely because of which front door was used.

    The subtle part is the loader. project_history.py is imported by path, and @dataclass
    resolves its own module out of sys.modules while the class body runs: skip the
    registration and the whole module fails with a bare
    "AttributeError: 'NoneType' object has no attribute '__dict__'" that names nothing.
    That is exactly how this shipped broken once, reporting a green build with silently no
    history, so the import contract is checked here rather than only in a live build.
    """
    import importlib.util

    tool = Path(__file__).resolve().parent.parent / "pylib" / "project_history.py"
    assert tool.is_file(), f"project history module missing: {tool}"

    spec = importlib.util.spec_from_file_location("phicad_project_history_selftest", tool)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("phicad_project_history_selftest", module)
    spec.loader.exec_module(module)

    for name in ("prepare_generated_project", "ProjectHistory", "record_and_snapshot"):
        assert hasattr(module, name), f"project_history has no {name}()"

    # The CLI calls exactly this, and only ever best-effort.
    from inspect import signature
    params = signature(module.prepare_generated_project).parameters
    assert "project_dir" in params and "design_path" in params, params

    print("ok 12 - project history: the CLI's by-path import of the hook works")


def main() -> int:
    lib = Library.default()
    if not lib.roots or not any(root.is_dir() for root in lib.roots):
        print("selftest: no footprint libraries found; nothing to test against",
              file=sys.stderr)
        return 2

    check_library(lib)
    check_ir(lib)
    check_netlist(lib)
    check_place(lib)
    check_route(lib)
    check_emit(lib)
    check_silk(lib)
    check_symbols()
    check_ir_symbols(lib)
    check_schematic(lib)
    check_schematic_edit_guard(lib)
    check_board()
    check_project_history_hook()
    print("selftest: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
