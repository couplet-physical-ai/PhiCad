#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Translate Circuit JSON's electrical intent into the synth Design IR.

Circuit JSON is a build artifact, not a KiCad board file.  This translator deliberately has a
closed element policy.  The native materializer may use a semantic element, explicitly ignore a
purely visual/diagnostic element, or reject it; it never silently drops an element it cannot
represent in Design IR.  In particular, this module does not write footprint s-expressions.
KiCad's ImportNetlist path and :mod:`phicad.synth.emit`'s lossless text edits remain the only
native writers.

Coordinates cross one boundary here.  They are checked and rounded to integer nanometres with
``to_nm`` and all plan operations use those integers.  The existing synth pipeline consumes mm,
so the IR is reconstructed from the checked integer value immediately before that hand-off; no
other input coordinate conversion is performed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


class Rejected(RuntimeError):
    """A typed, user-actionable materialization rejection."""

    def __init__(self, code: str, detail: str = "", element: str = "") -> None:
        self.code = code
        self.detail = detail or code
        self.element = element
        super().__init__(self.detail)


# This is the one units boundary.  Keep the implementation in sync with the shared wave
# contract: round() is deliberately Python's half-even rounding.
def to_nm(mm: float) -> int:
    if not math.isfinite(mm):
        raise Rejected("non_finite_coordinate")
    nm = round(mm * 1_000_000)
    if abs(nm) > 2**47:
        raise Rejected("coordinate_overflow")
    return nm


def _mm(nm: int) -> float:
    """Present a checked nanometre value to the existing mm-based synth IR."""
    return nm / 1_000_000.0


@dataclass(frozen=True)
class ElementPolicy:
    action: str                         # mapped | ignored | rejected
    reason: str


# Closed by design.  Add a type here only after deciding whether its geometry is native,
# ignorable, or a typed rejection.  An absent key is an unknown-element-type rejection.
ELEMENT_POLICIES: Dict[str, ElementPolicy] = {
    "source_group": ElementPolicy("ignored", "group identity is not native board geometry"),
    "source_port": ElementPolicy("mapped", "port identity and pin number become connectivity"),
    "source_component": ElementPolicy("mapped", "component identity becomes a Design IR part"),
    "source_board": ElementPolicy("ignored", "PCB board geometry is authoritative"),
    "source_trace": ElementPolicy("mapped", "trace connectivity becomes a Design IR net"),
    "source_net": ElementPolicy("mapped", "source net identity becomes a Design IR net"),
    "source_pin_missing_trace_warning": ElementPolicy(
        "ignored", "engine diagnostic; an unconnected port is represented as no-connect"),
    "supplier_footprint_mismatch_warning": ElementPolicy(
        "ignored", "engine diagnostic; native footprint geometry is checked separately"),
    "source_project_metadata": ElementPolicy(
        "ignored", "engine provenance metadata is not native board geometry"),
    "schematic_component": ElementPolicy(
        "rejected", "schematic layout is not representable by the synth schematic emitter"),
    "schematic_port": ElementPolicy(
        "rejected", "schematic layout is not representable by the synth schematic emitter"),
    "schematic_group": ElementPolicy(
        "rejected", "schematic layout is not representable by the synth schematic emitter"),
    "schematic_trace": ElementPolicy(
        "rejected", "schematic layout is not representable by the synth schematic emitter"),
    "schematic_text": ElementPolicy(
        "ignored", "purely visual schematic annotation; native title/net labels are regenerated"),
    "schematic_missing_sheet_warning": ElementPolicy(
        "ignored", "engine diagnostic; the native schematic has its own sheet"),
    "pcb_component": ElementPolicy("mapped", "component placement becomes Design IR placement"),
    "pcb_board": ElementPolicy("mapped", "board dimensions and stackup become Design IR board"),
    "pcb_smtpad": ElementPolicy(
        "mapped", "pad identity is checked against the selected native footprint"),
    "pcb_port": ElementPolicy("mapped", "PCB port associates a pad with a source port"),
    "pcb_trace": ElementPolicy(
        "rejected", "Circuit JSON copper route is not representable by Design IR routing"),
    # These four are footprint OUTPUTS, so the decision is per element, not per type: attached to
    # a component whose package the pads pin exactly, the native footprint emits the same artifact
    # and it is ignored with a reason; free-standing, it is intent the Design IR cannot express and
    # it is rejected.  See the footprint-derived block in translate().
    "pcb_solder_paste": ElementPolicy(
        "mapped", "paste apertures are checked against this footprint's validated pads"),
    "pcb_silkscreen_path": ElementPolicy(
        "mapped", "package silkscreen is checked for an owning component"),
    "pcb_silkscreen_text": ElementPolicy(
        "mapped", "silkscreen text is checked against the component's reference designator"),
    "pcb_courtyard_rect": ElementPolicy(
        "mapped", "courtyard is checked for an owning component"),
    "pcb_fabrication_note_path": ElementPolicy(
        "ignored", "fabrication note is visual annotation, not native copper or connectivity"),
    "cad_component": ElementPolicy(
        "mapped", "2D placement and footprint identity are mapped; the 3D body is checked"),
    "pcb_via": ElementPolicy("rejected", "via geometry is not representable by the Design IR route stage"),
    "pcb_zone": ElementPolicy("rejected", "zone geometry is not representable by synth ground pour"),
    "pcb_keepout": ElementPolicy("rejected", "keepout geometry is not representable by Design IR"),
    "pcb_hole": ElementPolicy("rejected", "hole geometry is not representable by Design IR"),
    "pcb_plated_hole": ElementPolicy("rejected", "hole geometry is not representable by Design IR"),
    "pcb_cutout": ElementPolicy("rejected", "cutout geometry is not representable by Design IR"),
    "pcb_slot": ElementPolicy("rejected", "slot geometry is not representable by Design IR"),
}

# Coordinates and dimensions in Circuit JSON are named rather than all appearing at one level.
# Checking these keys recursively also protects ignored and rejected elements: a malformed
# element cannot hide NaN behind a policy decision.
_GEOMETRY_KEYS = frozenset({
    "x", "y", "z", "width", "height", "thickness", "stroke_width", "font_size",
    "display_offset_x", "display_offset_y", "min_trace_width", "min_via_hole_diameter",
    "min_via_pad_diameter", "min_via_hole_edge_to_via_hole_edge_clearance",
    "min_trace_to_pad_edge_clearance", "min_pad_edge_to_pad_edge_clearance",
    "min_plated_hole_drill_edge_to_drill_edge_clearance", "min_board_edge_clearance",
})

_LAYER_NAMES = {
    "top": "F.Cu", "bottom": "B.Cu", "f.cu": "F.Cu", "b.cu": "B.Cu",
    "F.Cu": "F.Cu", "B.Cu": "B.Cu",
}

_REF_RE = re.compile(r"^[A-Za-z]+[0-9]+$")
_PIN_RE = re.compile(r"^([A-Za-z]+[0-9]+)\.([^\.]+)$")

# The stock-library geometry the synth pipeline can prove.  A Circuit JSON pad is accepted only
# when it agrees with this geometry, so a convenient-but-wrong standard footprint is not chosen.
# Values are (relative x, relative y, width, height), in mm, for pad 1 then pad 2.
_FOOTPRINTS: Dict[str, Tuple[str, str, str, Tuple[Tuple[float, float, float, float], ...]]] = {
    "simple_resistor:0402": (
        "Resistor_SMD:R_0402_1005Metric", "Device:R", "0402",
        ((-0.51, 0.0, 0.54, 0.64), (0.51, 0.0, 0.54, 0.64))),
    "simple_resistor:0603": (
        "Resistor_SMD:R_0603_1608Metric", "Device:R", "0603",
        ((-0.825, 0.0, 0.80, 0.95), (0.825, 0.0, 0.80, 0.95))),
    "simple_resistor:0805": (
        "Resistor_SMD:R_0805_2012Metric", "Device:R", "0805", ()),
    "simple_resistor:1206": (
        "Resistor_SMD:R_1206_3216Metric", "Device:R", "1206", ()),
    "simple_capacitor:0402": (
        "Capacitor_SMD:C_0402_1005Metric", "Device:C", "0402", ()),
    "simple_capacitor:0603": (
        "Capacitor_SMD:C_0603_1608Metric", "Device:C", "0603", ()),
    "simple_inductor:0402": (
        "Inductor_SMD:L_0402_1005Metric", "Device:L", "0402", ()),
    "simple_inductor:0603": (
        "Inductor_SMD:L_0603_1608Metric", "Device:L", "0603", ()),
    "simple_diode:0402": (
        "Diode_SMD:D_0402_1005Metric", "Device:D", "0402", ()),
    "simple_diode:0603": (
        "Diode_SMD:D_0603_1608Metric", "Device:D", "0603", ()),
    "simple_led:0603": (
        "LED_SMD:LED_0603_1608Metric", "Device:LED", "0603", ()),
}


@dataclass
class Translated:
    """A checked translation and the evidence needed by apply-plan."""

    design_ir: Dict[str, Any]
    operations: List[Dict[str, Any]]
    counts: Dict[str, int]
    supported: List[str]
    rejected_types: List[str]
    rejections: List[Dict[str, str]]
    ignored: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.rejections


@dataclass
class _CheckedElement:
    element: Mapping[str, Any]
    coordinates: Dict[str, int]


def _reject(code: str, element: str, detail: str) -> None:
    raise Rejected(code, detail, element)


def _walk_coordinates(value: Any, path: str, out: Dict[str, int]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        # Numeric non-coordinate values still fail closed when they are non-finite.  This avoids
        # Python's permissive json NaN handling producing a non-deterministic IR later.
        if isinstance(value, float) and not math.isfinite(value):
            raise Rejected("non_finite_coordinate")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in _GEOMETRY_KEYS and isinstance(child, (int, float)) \
                    and not isinstance(child, bool):
                try:
                    out[child_path] = to_nm(float(child))
                except Rejected as exc:
                    exc.element = path.split(".", 1)[0]
                    raise
            elif key == "rotation" and isinstance(child, (int, float)):
                if not math.isfinite(float(child)):
                    raise Rejected("non_finite_coordinate")
            else:
                _walk_coordinates(child, child_path, out)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_coordinates(child, f"{path}[{index}]", out)


def _checked(element: Mapping[str, Any]) -> _CheckedElement:
    coords: Dict[str, int] = {}
    _walk_coordinates(element, str(element.get("type", "element")), coords)
    return _CheckedElement(element, coords)


def _coord(checked: _CheckedElement, suffix: str, *, required: bool = True,
           default: Optional[int] = None) -> int:
    for path, value in checked.coordinates.items():
        if path.endswith("." + suffix):
            return value
    if required:
        _reject("unsupported_required_geometry", str(checked.element.get("type")),
                f"missing coordinate {suffix}")
    return 0 if default is None else default


def _xy(checked: _CheckedElement, key: str = "") -> Tuple[int, int]:
    prefix = f".{key}" if key else ""
    x = next((v for p, v in checked.coordinates.items() if p.endswith(prefix + ".x")), None)
    y = next((v for p, v in checked.coordinates.items() if p.endswith(prefix + ".y")), None)
    if x is None or y is None:
        # The simple element shape has x/y at its root.  The suffix search handles both it and
        # nested center/position objects without adding another conversion boundary.
        x = next((v for p, v in checked.coordinates.items() if p.endswith(".x")), None)
        y = next((v for p, v in checked.coordinates.items() if p.endswith(".y")), None)
    if x is None or y is None:
        _reject("unsupported_required_geometry", str(checked.element.get("type")),
                "missing x/y coordinate")
    return int(x), int(y)


def _layer(value: Any, element: str) -> str:
    if not isinstance(value, str) or value not in _LAYER_NAMES:
        _reject("invalid_layer", element, f"unsupported layer {value!r}; expected top/bottom")
    return _LAYER_NAMES[value]


def _near(a: int, b: int, tolerance: int = 1) -> bool:
    return abs(a - b) <= tolerance


def _footprint_key(ftype: str, footprinter: str) -> str:
    if ftype not in {"simple_resistor", "simple_capacitor", "simple_inductor",
                     "simple_diode", "simple_led"}:
        raise Rejected("unsupported_required_geometry", f"component type {ftype!r} is not supported")
    match = re.search(r"(01005|0201|0402|0603|0805|1206|1210)", footprinter.lower())
    if not match:
        raise Rejected("unsupported_required_geometry",
                       f"footprinter {footprinter!r} has no supported metric size")
    key = f"{ftype}:{match.group(1)}"
    if key not in _FOOTPRINTS:
        raise Rejected("unsupported_required_geometry",
                       f"no lossless native footprint mapping for {ftype}/{match.group(1)}")
    return key


def _source_id(element: Mapping[str, Any]) -> str:
    value = element.get("source_component_id")
    if not isinstance(value, str) or not value:
        _reject("unsupported_required_geometry", "source_component", "source_component_id is required")
    return value


def _sanitize_net(name: str, used: Iterable[str]) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.+-]+", "_", name).strip("_") or "NET"
    if clean[0].isdigit():
        clean = "N_" + clean
    taken = set(used)
    result = clean
    index = 2
    while result in taken:
        result = f"{clean}_{index}"
        index += 1
    return result


def _rotate_xy(x: int, y: int, degrees: float) -> Tuple[int, int]:
    radians = math.radians(degrees)
    c, s = math.cos(radians), math.sin(radians)
    return round(x * c - y * s), round(x * s + y * c)


def _validate_pad_geometry(
    element: Mapping[str, Any],
    checked: _CheckedElement,
    expected: Sequence[Tuple[float, float, float, float]],
    component_center: Tuple[int, int],
    rotation: float,
    pad_number: str,
) -> None:
    if not expected:
        return
    try:
        index = int(pad_number) - 1
    except ValueError:
        _reject("unsupported_required_geometry", "pcb_smtpad",
                f"native footprint pad number {pad_number!r} is not numeric")
    if index < 0 or index >= len(expected):
        _reject("unsupported_required_geometry", "pcb_smtpad",
                f"native footprint has no pad {pad_number!r}")

    x, y = _xy(checked)
    width = next((v for p, v in checked.coordinates.items() if p.endswith(".width")), None)
    height = next((v for p, v in checked.coordinates.items() if p.endswith(".height")), None)
    if width is None or height is None:
        _reject("unsupported_required_geometry", "pcb_smtpad",
                "pad width and height are required to prove native footprint geometry")
    exp_x, exp_y, exp_w, exp_h = expected[index]
    dx, dy = x - component_center[0], y - component_center[1]
    rdx, rdy = _rotate_xy(to_nm(exp_x), to_nm(exp_y), rotation)
    if not (_near(dx, rdx) and _near(dy, rdy)
            and _near(width, to_nm(exp_w)) and _near(height, to_nm(exp_h))):
        _reject(
            "unsupported_required_geometry", "pcb_smtpad",
            f"pad {pad_number} geometry does not exactly match the selected native footprint",
        )


def _add_rejection(rejections: Dict[Tuple[str, str], Dict[str, str]], exc: Rejected,
                   element_type: str) -> None:
    detail = exc.detail
    key = (exc.code, element_type)
    rejections.setdefault(key, {"code": exc.code, "element": element_type, "detail": detail})


def translate(circuit_json: Sequence[Mapping[str, Any]], project: str,
              *, board_name: Optional[str] = None) -> Translated:
    """Translate a parsed Circuit JSON list, returning deterministic apply evidence."""
    if not isinstance(circuit_json, list):
        raise Rejected("invalid_circuit_json", "Circuit JSON root must be an array")
    if len(circuit_json) > 100_000:
        raise Rejected("circuit_json_too_many_elements", "Circuit JSON exceeds 100000 elements")

    checked: List[_CheckedElement] = []
    rejections: Dict[Tuple[str, str], Dict[str, str]] = {}
    # Field-level ignores: a policy decision about ONE field of an otherwise mapped element.
    # These are reported in the plan as `ignore_field` operations so a reviewer sees every
    # deliberate omission, not just the element types that were dropped wholesale.
    ignored_fields: Dict[str, str] = {}
    # Element types ignored because a native footprint already emits them (see the
    # footprint-derived block below).  Conditional, so they cannot live in ELEMENT_POLICIES.
    derived_ignores: Dict[str, str] = {}
    validated_pads: set[str] = set()
    seen_types: set[str] = set()
    for raw in circuit_json:
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            _add_rejection(rejections, Rejected("unknown_element_type", "element has no string type"),
                           "<missing>")
            continue
        typ = raw["type"]
        seen_types.add(typ)
        policy = ELEMENT_POLICIES.get(typ)
        if policy is None:
            _add_rejection(rejections, Rejected("unknown_element_type",
                                               f"element type {typ!r} is not allowlisted"), typ)
            continue
        try:
            item = _checked(raw)
        except Rejected as exc:
            _add_rejection(rejections, exc, typ)
            continue
        checked.append(item)
        if policy.action == "rejected":
            _add_rejection(rejections, Rejected("unsupported_required_geometry", policy.reason), typ)

    by_type: Dict[str, List[_CheckedElement]] = {}
    for item in checked:
        by_type.setdefault(str(item.element["type"]), []).append(item)

    # Basic census is meaningful even on a rejected plan.
    counts = {
        "components": len(by_type.get("source_component", [])),
        "pads": len(by_type.get("pcb_smtpad", [])),
        "nets": 0,
        "traces": len(by_type.get("pcb_trace", [])),
        "vias": len(by_type.get("pcb_via", [])),
        "zones": len(by_type.get("pcb_zone", [])),
    }

    board_elements = by_type.get("pcb_board", [])
    board = board_elements[0].element if board_elements else None
    if board is None:
        _add_rejection(rejections, Rejected("unsupported_required_geometry", "pcb_board is required"),
                       "pcb_board")
        board_center = (0, 0)
        board_width = board_height = to_nm(10.0)
        board_layers = 2
        board_thickness = to_nm(1.6)
    else:
        if len(board_elements) != 1:
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "Circuit JSON must contain exactly one pcb_board"),
                           "pcb_board")
        board_checked = board_elements[0]
        try:
            board_center = _xy(board_checked, "center")
            board_width = _coord(board_checked, "width")
            board_height = _coord(board_checked, "height")
            board_thickness = _coord(board_checked, "thickness")
            board_layers = int(board.get("num_layers", 2))
            if board_layers not in (1, 2, 4, 6, 8):
                _reject("invalid_layer", "pcb_board", f"unsupported num_layers {board_layers}")
        except (Rejected, ValueError, TypeError) as exc:
            if isinstance(exc, Rejected):
                _add_rejection(rejections, exc, "pcb_board")
            else:
                _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                                   f"invalid board stackup: {exc}"), "pcb_board")
            board_center = (0, 0)
            board_width = board_height = to_nm(10.0)
            board_layers = 2
            board_thickness = to_nm(1.6)

    components: Dict[str, Mapping[str, Any]] = {}
    component_ids: Dict[str, str] = {}
    for item in by_type.get("source_component", []):
        element = item.element
        ref = element.get("name")
        sid = element.get("source_component_id")
        if not isinstance(ref, str) or not _REF_RE.match(ref) or not isinstance(sid, str):
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "component name and id must be valid"),
                           "source_component")
            continue
        if ref in components:
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               f"duplicate component reference {ref}"),
                           "source_component")
            continue
        components[ref] = element
        component_ids[sid] = ref

    cad_by_source: Dict[str, Mapping[str, Any]] = {}
    for item in by_type.get("cad_component", []):
        source_id = item.element.get("source_component_id")
        if isinstance(source_id, str):
            cad_by_source[source_id] = item.element

    pcb_components: Dict[str, Mapping[str, Any]] = {}
    pcb_center_by_source: Dict[str, Tuple[int, int]] = {}
    pcb_layer_by_source: Dict[str, str] = {}
    pcb_rotation_by_source: Dict[str, float] = {}
    pcb_to_source: Dict[str, str] = {}
    for item in by_type.get("pcb_component", []):
        element = item.element
        pcb_id = element.get("pcb_component_id")
        source_id = element.get("source_component_id")
        if not isinstance(pcb_id, str) or not isinstance(source_id, str):
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "pcb_component ids are required"), "pcb_component")
            continue
        try:
            centre = _xy(item, "center")
            layer = _layer(element.get("layer"), "pcb_component")
            rotation = float(element.get("rotation", 0.0))
            if not math.isfinite(rotation):
                raise Rejected("non_finite_coordinate")
        except Rejected as exc:
            _add_rejection(rejections, exc, "pcb_component")
            continue
        pcb_components[pcb_id] = element
        pcb_to_source[pcb_id] = source_id
        pcb_center_by_source[source_id] = centre
        pcb_layer_by_source[source_id] = layer
        pcb_rotation_by_source[source_id] = rotation

    source_ports: Dict[str, Mapping[str, Any]] = {}
    for item in by_type.get("source_port", []):
        sid = item.element.get("source_port_id")
        source_id = item.element.get("source_component_id")
        pin = item.element.get("pin_number")
        if not isinstance(sid, str) or not isinstance(source_id, str) or not isinstance(pin, (int, str)):
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "source port id, component id and pin_number are required"),
                           "source_port")
            continue
        source_ports[sid] = item.element

    pcb_ports: Dict[str, Mapping[str, Any]] = {}
    source_for_pcb_port: Dict[str, str] = {}
    for item in by_type.get("pcb_port", []):
        element = item.element
        pid = element.get("pcb_port_id")
        source_port_id = element.get("source_port_id")
        pcb_component_id = element.get("pcb_component_id")
        if not all(isinstance(v, str) for v in (pid, source_port_id, pcb_component_id)):
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "PCB port ids are required"), "pcb_port")
            continue
        try:
            _xy(item)
            for layer in element.get("layers", []):
                _layer(layer, "pcb_port")
        except Rejected as exc:
            _add_rejection(rejections, exc, "pcb_port")
            continue
        pcb_ports[pid] = element
        source_for_pcb_port[pid] = source_port_id

    pad_for_source_port: Dict[str, Mapping[str, Any]] = {}
    pad_numbers: Dict[str, str] = {}
    for item in by_type.get("pcb_smtpad", []):
        element = item.element
        pcb_component_id = element.get("pcb_component_id")
        pcb_port_id = element.get("pcb_port_id")
        if not isinstance(pcb_component_id, str) or not isinstance(pcb_port_id, str):
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "SMT pad must identify a PCB component and port"),
                           "pcb_smtpad")
            continue
        source_port_id = source_for_pcb_port.get(pcb_port_id)
        if source_port_id is None or source_port_id not in source_ports:
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "SMT pad port does not resolve to a source port"),
                           "pcb_smtpad")
            continue
        source_id = pcb_to_source.get(pcb_component_id)
        if source_id is None or source_id != source_ports[source_port_id].get("source_component_id"):
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               "SMT pad component and source port disagree"),
                           "pcb_smtpad")
            continue
        try:
            pin = str(source_ports[source_port_id]["pin_number"])
            cad = cad_by_source.get(source_id, {})
            key = _footprint_key(str(components[component_ids[source_id]].get("ftype", "")),
                                 str(cad.get("footprinter_string", "")))
            expected = _FOOTPRINTS[key][3]
            _validate_pad_geometry(item.element, item, expected,
                                   pcb_center_by_source[source_id],
                                   pcb_rotation_by_source.get(source_id, 0.0), pin)
            _layer(element.get("layer"), "pcb_smtpad")
        except (Rejected, KeyError) as exc:
            if isinstance(exc, Rejected):
                _add_rejection(rejections, exc, "pcb_smtpad")
            else:
                _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                                   f"pad mapping failed: {exc}"), "pcb_smtpad")
            continue
        pad_for_source_port[source_port_id] = element
        pad_numbers[source_port_id] = pin
        smtpad_id = element.get("pcb_smtpad_id")
        if isinstance(smtpad_id, str):
            validated_pads.add(smtpad_id)

    # ---------------------------------------------------------------- footprint-derived artifacts
    #
    # Courtyard, solder paste and per-component silkscreen are OUTPUTS of a footprint, not
    # independent design intent.  The native stock footprint emits its own, and the package
    # identity is already pinned exactly: every pad above had to match the selected native
    # footprint's pad geometry to within 1 nm, so "the 0402 courtyard" is not a guess.
    #
    # The split is therefore between DERIVED and INTENT, and it is drawn on whether the element
    # belongs to a component this translation mapped:
    #
    #   attached to a mapped component  -> derived, regenerated natively, ignored with a reason
    #   free-standing (board level)     -> intent the Design IR cannot express, REJECTED
    #
    # A board-level silk logo, a courtyard someone drew by hand or a paste aperture that is not a
    # pad's are all real intent, and all still reject.  This is the line the contract's "do not
    # approximate" rule is about: never drop intent, do regenerate what the library owns.
    def _mapped_component(element: Mapping[str, Any]) -> bool:
        pcb_id = element.get("pcb_component_id")
        if not isinstance(pcb_id, str):
            return False
        source_id = pcb_to_source.get(pcb_id)
        return source_id is not None and source_id in component_ids

    for type_name, reason in (
        ("pcb_courtyard_rect",
         "courtyard is emitted by the native footprint for the package the pads pin exactly"),
        ("pcb_solder_paste",
         "paste apertures are generated from the native footprint's own pads"),
        ("pcb_silkscreen_path",
         "package silkscreen is emitted by the native footprint"),
    ):
        for item in by_type.get(type_name, []):
            element = item.element
            if not _mapped_component(element):
                _add_rejection(rejections, Rejected(
                    "unsupported_required_geometry",
                    "board-level geometry with no owning component is not representable by the "
                    "Design IR"), type_name)
                continue
            if type_name == "pcb_solder_paste":
                paste_pad = element.get("pcb_smtpad_id")
                if not isinstance(paste_pad, str) or paste_pad not in validated_pads:
                    _add_rejection(rejections, Rejected(
                        "unsupported_required_geometry",
                        "a paste aperture that is not one of this footprint's validated pads is "
                        "not representable"), type_name)
                    continue
            derived_ignores[type_name] = reason

    for item in by_type.get("pcb_silkscreen_text", []):
        element = item.element
        text = element.get("text")
        if not _mapped_component(element) or not isinstance(text, str):
            _add_rejection(rejections, Rejected(
                "unsupported_required_geometry",
                "free-standing silkscreen text is not representable by the Design IR"),
                "pcb_silkscreen_text")
            continue
        ref = component_ids[pcb_to_source[str(element["pcb_component_id"])]]
        value = str(components[ref].get("name", ""))
        if text.strip() not in (ref, value):
            # Anything other than the reference designator is a marking someone chose, and the
            # native silk stage would not reproduce it.
            _add_rejection(rejections, Rejected(
                "unsupported_required_geometry",
                f"silkscreen text {text.strip()!r} is neither the reference designator nor the "
                "component value, so the native silk stage would not reproduce it"),
                "pcb_silkscreen_text")
            continue
        derived_ignores["pcb_silkscreen_text"] = (
            "the reference designator is emitted natively by the synth silk stage")

    # The 3D body.  This is the one place where the honest line is narrower than "reject anything
    # unfamiliar", so it is drawn explicitly:
    #
    #  * a CUSTOM model asset is REJECTED.  Attaching it would mean rewriting the footprint's
    #    `(model ...)` node, and that is exactly the lossy kipy round-trip this repo already
    #    measured (model=6 -> 0, `emit.py::set_positions`).  Silently dropping the caller's model
    #    instead would be the defect the contract names.
    #  * a TILTED or FLIPPED body is REJECTED: rotation about x or y has no native equivalent for
    #    a surface-mount part placed on a layer.
    #  * a body BELOW the board is REJECTED: negative z is not a placement this maps.
    #  * otherwise the z OFFSET IS IGNORED, with a reason.  The stock native footprint carries its
    #    own `(model ...)` node, positioned by the library for that exact package, and this
    #    pipeline never rewrites footprints -- so nothing is lost by not importing the engine's
    #    own offset for the same body.  Rejecting a whole design over a 3D preview offset would
    #    reject every real engine document while losing no information by accepting it.
    _MODEL_ASSET_KEYS = ("model_obj_url", "model_stl_url", "model_gltf_url", "model_wrl_url",
                         "model_step_url", "model_jscad")
    ignored_z = False
    for item in by_type.get("cad_component", []):
        element = item.element
        carried = [key for key in _MODEL_ASSET_KEYS if element.get(key) not in (None, "", {}, [])]
        if carried:
            _add_rejection(rejections, Rejected(
                "unsupported_required_geometry",
                "a custom 3D model (" + ", ".join(sorted(carried)) + ") cannot be attached without "
                "rewriting the footprint's (model ...) node, which is a lossy round-trip"),
                "cad_component")
            continue
        rotation = element.get("rotation")
        if isinstance(rotation, dict):
            for axis in ("x", "y"):
                value = rotation.get(axis)
                if value not in (None, 0, 0.0):
                    _add_rejection(rejections, Rejected(
                        "unsupported_required_geometry",
                        f"a body rotated about {axis} is not representable"), "cad_component")
                    break
        pos = element.get("position")
        if isinstance(pos, dict):
            z = pos.get("z")
            if isinstance(z, (int, float)) and math.isfinite(z):
                if z < 0:
                    _add_rejection(rejections, Rejected(
                        "unsupported_required_geometry",
                        "a body below the board surface is not representable"), "cad_component")
                elif z > 0:
                    ignored_z = True
    if ignored_z:
        ignored_fields["cad_component.position.z"] = (
            "the native footprint's own (model ...) placement is authoritative and is preserved "
            "exactly, because footprints are never rewritten")

    # Build net groups from source connectivity keys and source traces.  The PCB trace itself is
    # deliberately rejected above; the semantic source trace still supplies the net identity.
    parent: Dict[str, str] = {port_id: port_id for port_id in source_ports}

    def find(port_id: str) -> str:
        while parent[port_id] != port_id:
            parent[port_id] = parent[parent[port_id]]
            port_id = parent[port_id]
        return port_id

    def union(a: str, b: str) -> None:
        if a not in parent or b not in parent:
            return
        left, right = find(a), find(b)
        if left != right:
            parent[max(left, right)] = min(left, right)

    by_key: Dict[str, List[str]] = {}
    for port_id, port in source_ports.items():
        key = port.get("subcircuit_connectivity_map_key")
        if isinstance(key, str) and key:
            by_key.setdefault(key, []).append(port_id)
    for ids in by_key.values():
        for other in ids[1:]:
            union(ids[0], other)

    net_name_by_root: Dict[str, str] = {}
    used_names: List[str] = []
    for item in by_type.get("source_trace", []):
        element = item.element
        ids = [value for value in element.get("connected_source_port_ids", [])
               if isinstance(value, str) and value in parent]
        for other in ids[1:]:
            union(ids[0], other)
        if ids:
            name = str(element.get("name") or element.get("display_name") or "NET")
            root = find(ids[0])
            if root not in net_name_by_root:
                chosen = _sanitize_net(name, used_names)
                net_name_by_root[root] = chosen
                used_names.append(chosen)
    for item in by_type.get("source_net", []):
        element = item.element
        ids = [value for value in element.get("connected_source_port_ids", [])
               if isinstance(value, str) and value in parent]
        for other in ids[1:]:
            union(ids[0], other)
        if ids:
            root = find(ids[0])
            if root not in net_name_by_root:
                chosen = _sanitize_net(str(element.get("name") or "NET"), used_names)
                net_name_by_root[root] = chosen
                used_names.append(chosen)

    nets: Dict[str, List[str]] = {}
    no_connect: List[str] = []
    for source_port_id in sorted(source_ports):
        source_id = str(source_ports[source_port_id].get("source_component_id", ""))
        ref = component_ids.get(source_id)
        pin = pad_numbers.get(source_port_id)
        if not ref or not pin:
            continue
        root = find(source_port_id)
        name = net_name_by_root.get(root)
        pin_name = f"{ref}.{pin}"
        if name:
            nets.setdefault(name, []).append(pin_name)
        else:
            no_connect.append(pin_name)
    for name in nets:
        nets[name] = sorted(set(nets[name]))
    counts["nets"] = len(nets)

    parts: Dict[str, Dict[str, Any]] = {}
    placement: Dict[str, Dict[str, Any]] = {}
    operations: List[Dict[str, Any]] = []
    for ref in sorted(components):
        component = components[ref]
        source_id = component_ids.get(str(component.get("source_component_id", "")))
        if source_id is None:
            _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                               f"component {ref} has no source id"),
                           "source_component")
            continue
        cad = cad_by_source.get(str(component.get("source_component_id", "")), {})
        try:
            source_component_id = str(component.get("source_component_id", ""))
            key = _footprint_key(str(component.get("ftype", "")),
                                 str(cad.get("footprinter_string", "")))
            footprint, symbol, size, _expected = _FOOTPRINTS[key]
            cx, cy = pcb_center_by_source[source_component_id]
            layer = pcb_layer_by_source[source_component_id]
            rotation = pcb_rotation_by_source.get(source_component_id, 0.0)
            at_x = cx - board_center[0] + board_width // 2
            at_y = cy - board_center[1] + board_height // 2
            # Rotation is already a validated finite scalar; use its integer-degree form only
            # when it is mathematically integral so plan JSON stays stable.
            rot_value: Any = int(rotation) if rotation.is_integer() else rotation
            parts[ref] = {
                "footprint": footprint,
                "symbol": symbol,
                "value": str(component.get("display_resistance")
                           or component.get("symbol_display_value") or ref),
            }
            placement[ref] = {
                "at": [_mm(at_x), _mm(at_y)],
                "rot": rot_value,
                "side": "front" if layer == "F.Cu" else "back",
                "lock": True,
            }
            operations.append({
                "op": "create_footprint", "ref": ref, "layer": layer,
                "at_nm": [at_x, at_y], "rotation_deg": rot_value,
            })
        except (Rejected, KeyError) as exc:
            if isinstance(exc, Rejected):
                _add_rejection(rejections, exc, "source_component")
            else:
                _add_rejection(rejections, Rejected("unsupported_required_geometry",
                                                   f"placement for {ref} failed: {exc}"),
                               "source_component")

    for name in sorted(nets):
        operations.append({"op": "create_net", "name": name, "pins": nets[name]})
    for name, policy in sorted({typ: ELEMENT_POLICIES[typ].reason for typ in seen_types
                                if typ in ELEMENT_POLICIES
                                and ELEMENT_POLICIES[typ].action == "ignored"}.items()):
        operations.append({"op": "ignore_element", "element": name, "reason": policy})
    for name in sorted(ignored_fields):
        operations.append({"op": "ignore_field", "element": name, "reason": ignored_fields[name]})
    for name in sorted(derived_ignores):
        if name not in {entry["element"] for entry in rejections.values()}:
            operations.append({"op": "ignore_element", "element": name,
                               "reason": derived_ignores[name]})

    # Remove a type from supported if one of its elements was rejected.  The rejected list is
    # unique and sorted so a plan is stable even if the engine changes element order.
    rejection_types = sorted({entry["element"] for entry in rejections.values()})
    supported = sorted(typ for typ in seen_types if typ in ELEMENT_POLICIES
                      and typ not in rejection_types)

    board_checked = board_elements[0] if board_elements else None
    edge_clearance = (_coord(board_checked, "min_board_edge_clearance", required=False,
                             default=to_nm(0.5)) if board_checked else to_nm(0.5))
    pad_clearance = (_coord(board_checked, "min_pad_edge_to_pad_edge_clearance", required=False,
                            default=to_nm(0.2)) if board_checked else to_nm(0.2))
    min_trace = (_coord(board_checked, "min_trace_width", required=False,
                        default=to_nm(0.25)) if board_checked else to_nm(0.25))
    board_ir = {
        "layers": board_layers,
        "thickness": _mm(board_thickness),
        "outline": "rect",
        "width": _mm(board_width),
        "height": _mm(board_height),
        "margin": _mm(edge_clearance),
        "origin": [0.0, 0.0],
        "pour_ground": False,
    }
    rules_ir = {
        "clearance": max(_mm(pad_clearance), 0.05),
        "track_width": max(_mm(min_trace), 0.15),
        "min_track_width": _mm(min_trace),
        "edge_clearance": _mm(edge_clearance),
    }
    design_ir = {
        "project": board_name or project,
        "title": board_name or project,
        "rev": "A",
        "company": "Example Co",
        "board": board_ir,
        "rules": rules_ir,
        "parts": parts,
        "nets": nets,
        "no_connect": sorted(set(no_connect)),
        "placement": placement,
    }
    rejection_list = [rejections[key] for key in sorted(rejections)]
    return Translated(
        design_ir=design_ir,
        operations=sorted(operations, key=lambda op: (op.get("op", ""),
                                                        op.get("ref", op.get("name",
                                                        op.get("element", ""))))),
        counts=counts,
        supported=supported,
        rejected_types=rejection_types,
        rejections=rejection_list,
        ignored=dict(sorted(
            {typ: ELEMENT_POLICIES[typ].reason for typ in seen_types
             if typ in ELEMENT_POLICIES and ELEMENT_POLICIES[typ].action == "ignored"}.items()
            | ignored_fields.items()
            | {k: v for k, v in derived_ignores.items()
               if k not in rejection_types}.items())),
    )


__all__ = [
    "ELEMENT_POLICIES", "ElementPolicy", "Rejected", "Translated", "to_nm", "translate",
]
