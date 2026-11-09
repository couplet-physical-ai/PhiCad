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
"""Headless reader/writer for .kicad_sch and .kicad_sym files. One JSON answer per run.

WHY A WORKER AND NOT kipy
    The IPC API has no schematic surface worth using (kipy 0.8.0 can open a schematic and
    almost nothing else), and eeschema's own reader lives inside a kiface DSO that cannot be
    imported.  So schematic editing here is text editing — but text editing that a KiCad 10
    parser still accepts and that a human can still review.

WHY NOT kiutils / kicad-skip  (measured on demos/pic_programmer/pic_programmer.kicad_sch)
    kiutils 1.4.8    silently DROPS tokens it does not model: do_not_autoplace 641->0,
                     hide 391->0, exclude_from_sim 135->0, in_pos_files 126->0,
                     body_style 105->0, embedded_fonts 22->0, generator_version 1->0.
                     229174 bytes in, 178091 out.  Unusable for round-tripping.
    kicad-skip 0.2.5 keeps every token but reflows the whole file (16830 diff lines), so a
                     one-field edit is indistinguishable from a rewrite in review.
    So every read AND write here goes through phicad/pylib/sexp_edit.py, which parses to
    spans and splices: a no-op load+save is byte-identical, and setting one field changes
    exactly the characters of that field.

PROTOCOL
    sch_worker.py <command> '<json payload>'     ->  one JSON object on stdout
    exit 0 when the result has ok=true, 1 otherwise.  Nothing ever escapes as a traceback.

COMMANDS
    read:   list_symbols  get_symbol  annotate_check  list_sheets  find
            sym_lib_list  sym_lib_get  parse_netlist
    write:  set_fields  place_symbol  add_wire  add_label  add_no_connect  delete_symbol
            (every writer takes out_path; in-place only when out_path == the input path)

WHAT A SCHEMATIC REFERENCE ACTUALLY IS
    A symbol carries its refdes TWICE: in `(property "Reference" "U2")` (what is drawn) and
    in `(instances (project … (path … (reference "U2"))))` (what netlisting and the board
    read).  Setting the Reference field updates both; setting only one is how a schematic
    ends up annotated in the editor and unannotated in the netlist.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))

import sexp_edit as se        # noqa: E402  (the path juggling above is deliberate)

SCH_SUFFIX = ".kicad_sch"
SYM_SUFFIX = ".kicad_sym"

#: Fields eeschema creates for every symbol; anything else is a user/BOM field.
STOCK_FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "Description")

#: Label kinds accepted by add_label -> the node head each one writes.
LABEL_HEADS = {"local": "label", "global": "global_label", "hier": "hierarchical_label",
               "hierarchical": "hierarchical_label"}

#: Shapes a global/hierarchical label may declare.
LABEL_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")

#: KiCad's own line styles (common/stroke_params.cpp).
LINE_STYLES = ("default", "solid", "dash", "dash_dot", "dash_dot_dot", "dot")

MAX_MM = 10000.0          # a sheet is at most ~1.7 m; beyond this it is a units mistake


# ---------------------------------------------------------------------------
# result plumbing
# ---------------------------------------------------------------------------

def emit(payload, code=None):
    """Print one JSON object and exit. `ok` decides the exit code unless told otherwise."""
    if code is None:
        code = 0 if payload.get("ok") else 1
    json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()
    raise SystemExit(code)


def fail(msg, **extra):
    out = {"ok": False, "error": msg}
    out.update(extra)
    return out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mm(value, name):
    f = _num(value)
    if f is None:
        raise ValueError("%s must be a number, got %r" % (name, value))
    if abs(f) > MAX_MM:
        raise ValueError("%s=%s mm is outside any sheet (units mistake?)" % (name, f))
    return f


def _new_uuid():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# document access
# ---------------------------------------------------------------------------

def _load(path, *suffixes):
    p = Path(str(path)).expanduser()
    p = p.resolve() if p.is_absolute() else p.resolve()
    if not p.exists():
        raise ValueError("file not found: %s" % p)
    if suffixes and p.suffix not in suffixes:
        raise ValueError("unexpected file type %r, expected %s" % (p.suffix, list(suffixes)))
    doc = se.Doc.load(p)
    if p.suffix == SCH_SUFFIX and doc.root.head != "kicad_sch":
        raise ValueError("not a schematic document: root is (%s" % doc.root.head)
    if p.suffix == SYM_SUFFIX and doc.root.head != "kicad_symbol_lib":
        raise ValueError("not a symbol library: root is (%s" % doc.root.head)
    return p, doc


def _instances(doc):
    """Root-level `(symbol …)` nodes — placed instances, not cached definitions.

    `lib_symbols` nests its definitions one level down, and only a placement carries
    `(lib_id …)`, so this cannot confuse the two.
    """
    return [s for s in doc.root.children("symbol") if s.child("lib_id") is not None]


def _prop_nodes(node):
    """{name: property_node}, in file order."""
    out = {}
    for p in node.children("property"):
        args = p.atoms()
        if args:
            out[str(args[0])] = p
    return out


def _prop(node, name, default=None):
    for p in node.children("property"):
        args = p.atoms()
        if args and str(args[0]) == name:
            return str(args[1]) if len(args) >= 2 else ""
    return default


def _fields(node):
    out = {}
    for p in node.children("property"):
        args = p.atoms()
        if args:
            out[str(args[0])] = str(args[1]) if len(args) >= 2 else ""
    return out


def _instance_refs(sym):
    """Every `(reference …)` under this symbol's `(instances …)`, with its sheet path."""
    out = []
    inst = sym.child("instances")
    if inst is None:
        return out
    for project in inst.children("project"):
        pargs = project.atoms()
        pname = str(pargs[0]) if pargs else None
        for path in project.find_all("path"):
            args = path.atoms()
            ref = path.child("reference")
            unit = path.child("unit")
            out.append({
                "project": pname,
                "path": str(args[0]) if args else None,
                "reference": str(ref.atoms()[0]) if ref is not None and ref.atoms() else None,
                "unit": int(float(unit.atoms()[0])) if unit is not None and unit.atoms() else None,
                "_ref_node": ref,
            })
    return out


def _sym_ref(sym):
    """The refdes: the drawn Reference field, falling back to the instance reference."""
    ref = _prop(sym, "Reference")
    if ref:
        return ref
    for inst in _instance_refs(sym):
        if inst["reference"]:
            return inst["reference"]
    return None


def _at(node):
    vals = node.floats("at")
    if not vals:
        return None
    return {"x_mm": vals[0], "y_mm": vals[1] if len(vals) > 1 else None,
            "rotation": vals[2] if len(vals) > 2 else 0.0}


def _yesno(node, name):
    v = node.scalar(name)
    return None if v is None else v in ("yes", "true")


def _symbol_summary(sym, source=None):
    uid = sym.child("uuid")
    unit = sym.child("unit")
    out = {
        "ref": _sym_ref(sym),
        "value": _prop(sym, "Value"),
        "footprint": _prop(sym, "Footprint"),
        "lib_id": sym.scalar("lib_id"),
        "at": _at(sym),
        "unit": int(float(unit.atoms()[0])) if unit is not None and unit.atoms() else None,
        "body_style": sym.scalar("body_style"),
        "uuid": str(uid.atoms()[0]) if uid is not None and uid.atoms() else None,
        "in_bom": _yesno(sym, "in_bom"),
        "on_board": _yesno(sym, "on_board"),
        "dnp": _yesno(sym, "dnp"),
        "exclude_from_sim": _yesno(sym, "exclude_from_sim"),
    }
    if source:
        out["sheet_file"] = source
    return out


def _ref_sort_key(ref):
    m = re.match(r"^([^\d]*)(\d*)", ref or "")
    return (m.group(1), int(m.group(2)) if m.group(2) else -1) if m else (ref or "", -1)


# ---------------------------------------------------------------------------
# hierarchy
# ---------------------------------------------------------------------------

def _sheet_children(path, doc):
    """Direct `(sheet …)` nodes of one schematic, resolved to files on disk."""
    out = []
    for sh in doc.root.children("sheet"):
        fname = _prop(sh, "Sheetfile") or ""
        target = (path.parent / fname) if fname else None
        uid = sh.child("uuid")
        pages = []
        inst = sh.child("instances")
        if inst is not None:
            for p in inst.find_all("path"):
                page = p.child("page")
                if page is not None and page.atoms():
                    pages.append(str(page.atoms()[0]))
        out.append({
            "name": _prop(sh, "Sheetname") or "",
            "file": fname,
            "resolved_path": str(target) if target else None,
            "exists": bool(target and target.exists()),
            "uuid": str(uid.atoms()[0]) if uid is not None and uid.atoms() else None,
            "at": _at(sh),
            "size_mm": sh.floats("size"),
            "pins": [{"name": str(p.atoms()[0]) if p.atoms() else None,
                      "shape": str(p.atoms()[1]) if len(p.atoms()) > 1 else None,
                      "at": _at(p)} for p in sh.children("pin")],
            "pages": pages,
        })
    return out


def _hierarchy(path, doc, seen=None, depth=0):
    """The sheet tree rooted at one schematic file. Cycle-safe, depth-capped at 16."""
    seen = seen if seen is not None else set()
    key = str(path.resolve())
    node = {"file": str(path), "depth": depth, "sheets": []}
    if key in seen or depth > 16:
        node["revisit"] = True
        return node
    seen.add(key)
    for child in _sheet_children(path, doc):
        entry = dict(child)
        if child["exists"]:
            try:
                sub_path, sub = _load(child["resolved_path"], SCH_SUFFIX)
                entry["subtree"] = _hierarchy(sub_path, sub, seen, depth + 1)
            except ValueError as exc:
                entry["error"] = str(exc)
        node["sheets"].append(entry)
    return node


def _sheet_files(path, doc, seen=None):
    """[(file, Doc)] for the whole hierarchy, root first, each file visited once."""
    seen = seen if seen is not None else set()
    key = str(path.resolve())
    if key in seen:
        return []
    seen.add(key)
    out = [(path, doc)]
    for child in _sheet_children(path, doc):
        if not child["exists"]:
            continue
        try:
            sub_path, sub = _load(child["resolved_path"], SCH_SUFFIX)
        except ValueError:
            continue
        out.extend(_sheet_files(sub_path, sub, seen))
    return out


def _scope_files(path, doc, sheet):
    """Resolve the `sheet` selector to ([(path, doc)], note).

    None / ""       this file only (a .kicad_sch IS one sheet)
    "*" / "all"     the whole hierarchy reachable from this file
    <name|file>     the subsheet with that Sheetname or Sheetfile (this file if it matches)
    """
    if sheet in (None, "", "this", "self"):
        return [(path, doc)], "this file only"
    if sheet in ("*", "all", "hierarchy"):
        files = _sheet_files(path, doc)
        return files, "whole hierarchy (%d files)" % len(files)
    want = str(sheet)
    if want in (path.name, path.stem):
        return [(path, doc)], "this file only"
    for p, d in _sheet_files(path, doc):
        if p.name == want or p.stem == want:
            return [(p, d)], "sheet file %s" % p.name
    for parent, pdoc in _sheet_files(path, doc):
        for child in _sheet_children(parent, pdoc):
            if child["name"] == want and child["exists"]:
                sub_path, sub = _load(child["resolved_path"], SCH_SUFFIX)
                return [(sub_path, sub)], "sheet %r -> %s" % (want, sub_path.name)
    raise ValueError("no sheet named %r in this hierarchy" % sheet)


# ---------------------------------------------------------------------------
# symbol definitions (lib_symbols cache and .kicad_sym libraries)
# ---------------------------------------------------------------------------

_UNIT_SUFFIX = re.compile(r"^(?P<base>.+)_(?P<unit>\d+)_(?P<body>\d+)$")


def _pin_info(pin, unit=None, body=None):
    args = pin.atoms()
    name = pin.child("name")
    number = pin.child("number")
    at = pin.floats("at") or []
    return {
        "number": str(number.atoms()[0]) if number is not None and number.atoms() else None,
        "name": str(name.atoms()[0]) if name is not None and name.atoms() else None,
        "type": str(args[0]) if args else None,
        "style": str(args[1]) if len(args) > 1 else None,
        "at": {"x_mm": at[0] if at else None,
               "y_mm": at[1] if len(at) > 1 else None,
               "rotation": at[2] if len(at) > 2 else None},
        "length_mm": (pin.floats("length") or [None])[0],
        "unit": unit,
        "body_style": body,
        "hide": _yesno(pin, "hide"),
    }


def _definition_detail(sym, name):
    """Pins + properties + unit map of one symbol definition."""
    pins = []
    units = []
    for sub in sym.children("symbol"):
        args = sub.atoms()
        sub_name = str(args[0]) if args else ""
        m = _UNIT_SUFFIX.match(sub_name)
        unit = int(m.group("unit")) if m else None
        body = int(m.group("body")) if m else None
        graphics = {h: len(sub.children(h)) for h in
                    ("polyline", "rectangle", "circle", "arc", "text", "bezier")
                    if sub.children(h)}
        units.append({"name": sub_name, "unit": unit, "body_style": body,
                      "pins": len(sub.children("pin")), "graphics": graphics})
        for pin in sub.children("pin"):
            pins.append(_pin_info(pin, unit, body))
    for pin in sym.children("pin"):          # some libraries hang pins off the parent
        pins.append(_pin_info(pin))
    unit_numbers = sorted({u["unit"] for u in units if u["unit"] not in (None, 0)})
    pin_names = sym.child("pin_names")
    pin_numbers = sym.child("pin_numbers")
    return {
        "name": name,
        "extends": sym.scalar("extends"),
        "is_power": sym.child("power") is not None,
        "pin_names_offset_mm": (pin_names.floats("offset") or [None])[0]
        if pin_names is not None else None,
        "pin_numbers_hidden": (pin_numbers is not None
                               and _yesno(pin_numbers, "hide") is not False),
        "in_bom": _yesno(sym, "in_bom"),
        "on_board": _yesno(sym, "on_board"),
        "exclude_from_sim": _yesno(sym, "exclude_from_sim"),
        "properties": _fields(sym),
        "unit_count": len(unit_numbers) or (1 if units else 0),
        "units": units,
        "pin_count": len(pins),
        "pins": pins,
    }


def _lib_symbols(doc):
    """{name: node} of a schematic's cached `(lib_symbols …)` definitions."""
    block = doc.root.child("lib_symbols")
    out = {}
    if block is None:
        return out
    for sym in block.children("symbol"):
        args = sym.atoms()
        if args:
            out[str(args[0])] = sym
    return out


def _unit_pin_numbers(defn, unit):
    """Pin numbers of one unit of a definition (unit 0 = shared, always included)."""
    numbers = []
    for sub in defn.children("symbol"):
        args = sub.atoms()
        m = _UNIT_SUFFIX.match(str(args[0]) if args else "")
        sub_unit = int(m.group("unit")) if m else None
        if sub_unit not in (0, None) and unit is not None and sub_unit != unit:
            continue
        for pin in sub.children("pin"):
            n = pin.child("number")
            if n is not None and n.atoms():
                numbers.append(str(n.atoms()[0]))
    return numbers


# ---------------------------------------------------------------------------
# read commands
# ---------------------------------------------------------------------------

def cmd_list_symbols(req):
    """Placed symbols: refs, values, footprints, lib_ids, positions, uuids."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    files, note = _scope_files(path, doc, req.get("sheet"))
    rows = []
    for p, d in files:
        label = p.name if len(files) > 1 else None
        for sym in _instances(d):
            rows.append(_symbol_summary(sym, label))
    rows.sort(key=lambda r: (_ref_sort_key(r["ref"]), r["unit"] or 0))
    power = [r for r in rows if (r["ref"] or "").startswith("#")]
    return {"ok": True, "path": str(path), "scope": note,
            "files": [str(p) for p, _ in files],
            "symbol_count": len(rows), "power_symbol_count": len(power),
            "unique_refs": len({r["ref"] for r in rows if r["ref"]}),
            "symbols": rows}


def cmd_get_symbol(req):
    """One symbol (every unit): all fields, pin uuids, instances, library definition."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    files, note = _scope_files(path, doc, req.get("sheet", "*"))
    ref = str(req["ref"])
    hits = []
    for p, d in files:
        defs = _lib_symbols(d)
        for sym in _instances(d):
            if _sym_ref(sym) != ref:
                continue
            entry = _symbol_summary(sym, p.name)
            entry["fields"] = _fields(sym)
            entry["custom_fields"] = {k: v for k, v in entry["fields"].items()
                                      if k not in STOCK_FIELDS}
            entry["pin_uuids"] = {
                str(pin.atoms()[0]): (str(pin.child("uuid").atoms()[0])
                                      if pin.child("uuid") is not None
                                      and pin.child("uuid").atoms() else None)
                for pin in sym.children("pin") if pin.atoms()}
            entry["instances"] = [{k: v for k, v in i.items() if not k.startswith("_")}
                                  for i in _instance_refs(sym)]
            defn = defs.get(entry["lib_id"])
            entry["definition"] = _definition_detail(defn, entry["lib_id"]) if defn else None
            entry["definition_cached"] = defn is not None
            hits.append(entry)
    if not hits:
        return fail("no symbol with reference %r" % ref, path=str(path), scope=note)
    return {"ok": True, "path": str(path), "scope": note, "ref": ref,
            "unit_count": len(hits), "symbols": hits}


def cmd_annotate_check(req):
    """Unannotated refs (R?, U?) and duplicate (ref, unit) pairs across the scope."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    files, note = _scope_files(path, doc, req.get("sheet", "*"))
    unannotated, seen, duplicates, mismatched, missing = [], {}, [], [], []
    total = 0
    for p, d in files:
        for sym in _instances(d):
            total += 1
            ref = _sym_ref(sym)
            unit_node = sym.child("unit")
            unit = (int(float(unit_node.atoms()[0]))
                    if unit_node is not None and unit_node.atoms() else None)
            where = {"sheet_file": p.name, "lib_id": sym.scalar("lib_id"),
                     "at": _at(sym), "unit": unit}
            if not ref:
                missing.append(where)
                continue
            if "?" in ref:
                unannotated.append(dict(where, ref=ref))
            drawn = _prop(sym, "Reference")
            for inst in _instance_refs(sym):
                if drawn and inst["reference"] and inst["reference"] != drawn:
                    mismatched.append(dict(where, ref=drawn,
                                           instance_reference=inst["reference"],
                                           instance_path=inst["path"]))
            key = (ref, unit)
            if key in seen:
                duplicates.append({"ref": ref, "unit": unit,
                                   "first": seen[key], "second": where})
            else:
                seen[key] = where
    return {"ok": True, "path": str(path), "scope": note,
            "files": [str(p) for p, _ in files], "symbol_count": total,
            "annotated": not (unannotated or duplicates or missing),
            "unannotated_count": len(unannotated), "unannotated": unannotated,
            "duplicate_count": len(duplicates), "duplicates": duplicates,
            "missing_reference_count": len(missing), "missing_reference": missing,
            "field_instance_mismatch_count": len(mismatched),
            "field_instance_mismatch": mismatched,
            "note": "a multi-unit part legitimately repeats a ref with a different unit; "
                    "only a repeated (ref, unit) pair is a duplicate"}


def cmd_list_sheets(req):
    """The sheet hierarchy from this file down, with pins, pages and missing files."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    tree = _hierarchy(path, doc)
    files = _sheet_files(path, doc)
    flat = []
    for p, d in files:
        uid = d.root.child("uuid")
        flat.append({"file": str(p), "name": p.stem,
                     "symbol_count": len(_instances(d)),
                     "sheet_count": len(d.root.children("sheet")),
                     "uuid": str(uid.atoms()[0]) if uid is not None and uid.atoms() else None})
    missing = [s["file"] for s in _sheet_children(path, doc) if not s["exists"]]
    return {"ok": True, "path": str(path), "root": str(path), "tree": tree,
            "files": flat, "file_count": len(flat), "missing_sheet_files": missing,
            "is_flat": len(flat) == 1}


def cmd_find(req):
    """Substring (or regex) search over refs, lib_ids and every field name/value."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    files, note = _scope_files(path, doc, req.get("sheet", "*"))
    query = str(req.get("query", ""))
    if not query:
        return fail("query is required")
    use_re = bool(req.get("regex"))
    if use_re:
        try:
            pat = re.compile(query, re.I)
        except re.error as exc:
            return fail("bad regex: %s" % exc)

        def match(s):
            return bool(pat.search(s or ""))
    else:
        needle = query.lower()

        def match(s):
            return needle in (s or "").lower()

    hits = []
    for p, d in files:
        for sym in _instances(d):
            fields = _fields(sym)
            where = []
            if match(_sym_ref(sym)):
                where.append("ref")
            if match(sym.scalar("lib_id")):
                where.append("lib_id")
            for name, value in fields.items():
                if match(value):
                    where.append("field:%s" % name)
                elif match(name) and name not in STOCK_FIELDS:
                    where.append("field_name:%s" % name)
            if where:
                entry = _symbol_summary(sym, p.name)
                entry["matched_in"] = sorted(set(where))
                entry["fields"] = fields
                hits.append(entry)
    hits.sort(key=lambda r: _ref_sort_key(r["ref"]))
    limit = max(1, int(req.get("limit") or 200))
    return {"ok": True, "path": str(path), "scope": note, "query": query, "regex": use_re,
            "match_count": len(hits), "truncated": len(hits) > limit,
            "matches": hits[:limit]}


def cmd_sym_lib_list(req):
    """Every top-level symbol in a .kicad_sym: units, pin counts, key properties."""
    path, doc = _load(req["path"], SYM_SUFFIX)
    rows = []
    for sym in doc.root.children("symbol"):
        args = sym.atoms()
        name = str(args[0]) if args else None
        detail = _definition_detail(sym, name)
        props = detail["properties"]
        rows.append({"name": name, "extends": detail["extends"],
                     "is_power": detail["is_power"], "unit_count": detail["unit_count"],
                     "pin_count": detail["pin_count"],
                     "reference_prefix": props.get("Reference"),
                     "value": props.get("Value"), "footprint": props.get("Footprint"),
                     "datasheet": props.get("Datasheet"),
                     "description": props.get("Description"),
                     "keywords": props.get("ki_keywords"),
                     "fp_filters": props.get("ki_fp_filters")})
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return {"ok": True, "path": str(path), "library": path.stem,
            "version": doc.root.scalar("version"),
            "generator": doc.root.scalar("generator"),
            "symbol_count": len(rows), "symbols": rows}


def cmd_sym_lib_get(req):
    """One .kicad_sym definition: pins (number/name/type/position) and properties."""
    path, doc = _load(req["path"], SYM_SUFFIX)
    want = str(req["name"])
    names = []
    for sym in doc.root.children("symbol"):
        args = sym.atoms()
        name = str(args[0]) if args else ""
        names.append(name)
        if name == want:
            detail = _definition_detail(sym, name)
            detail["ok"] = True
            detail["path"] = str(path)
            detail["library"] = path.stem
            detail["lib_id"] = "%s:%s" % (path.stem, name)
            if detail["extends"]:
                detail["inherit_note"] = ("this symbol extends %r — its pins and graphics "
                                          "live in the parent" % detail["extends"])
            return detail
    close = [n for n in names if want.lower() in n.lower()]
    return fail("no symbol %r in %s" % (want, path.name), symbol_count=len(names),
                did_you_mean=close[:10])


def cmd_parse_netlist(req):
    """Parse a `sch export netlist --format kicadsexpr` file into nets -> pins."""
    p = Path(str(req["path"])).expanduser().resolve()
    if not p.exists():
        return fail("netlist not found: %s" % p)
    root = se.Doc.load(p).root
    if root.head != "export":
        return fail("not a kicadsexpr netlist: root is (%s" % root.head)
    comps = []
    block = root.child("components")
    if block is not None:
        for comp in block.children("comp"):
            libsource = comp.child("libsource")
            sheetpath = comp.child("sheetpath")
            comps.append({"ref": comp.scalar("ref"), "value": comp.scalar("value"),
                          "footprint": comp.scalar("footprint"),
                          "lib_id": libsource.scalar("part") if libsource is not None else None,
                          "sheet": sheetpath.scalar("names") if sheetpath is not None else None})
    nets = []
    nets_block = root.child("nets")
    for net in (nets_block.children("net") if nets_block is not None else []):
        pins = [{"ref": n.scalar("ref"), "pin": n.scalar("pin"),
                 "pin_function": n.scalar("pinfunction"),
                 "pin_type": n.scalar("pintype")} for n in net.children("node")]
        nets.append({"code": net.scalar("code"), "name": net.scalar("name"),
                     "pin_count": len(pins), "pins": pins})
    nets.sort(key=lambda n: -n["pin_count"])
    single = [n["name"] for n in nets if n["pin_count"] == 1]
    return {"ok": True, "path": str(p), "net_count": len(nets),
            "component_count": len(comps),
            "pin_connections": sum(n["pin_count"] for n in nets),
            "single_pin_nets": single, "single_pin_net_count": len(single),
            "biggest_nets": [{"name": n["name"], "pin_count": n["pin_count"]}
                             for n in nets[:10]],
            "nets": nets, "components": comps}


# ---------------------------------------------------------------------------
# write plumbing
# ---------------------------------------------------------------------------

def _out_target(path, req):
    """Where a writer writes: out_path if given, else <stem>.edited<suffix> beside it."""
    requested = req.get("out_path")
    if requested:
        out = Path(str(requested)).expanduser()
        if not out.is_absolute():
            out = (path.parent / out).resolve()
    else:
        out = path.with_name("%s.edited%s" % (path.stem, path.suffix))
    out.parent.mkdir(parents=True, exist_ok=True)
    return out, out.resolve() == path.resolve()


def _finish(path, doc, req, changes, extra=None):
    """Render the queued edits, write them out, and describe exactly what moved."""
    out, in_place = _out_target(path, req)
    before = doc.text
    try:
        rendered = doc.render()
    except se.EditConflict as exc:
        return fail("conflicting edits: %s" % exc, path=str(path))
    try:
        written = doc.save(out)
    except se.StaleWrite as exc:
        # Another writer changed the file after we read it. Refuse rather than silently
        # discarding their edit; the caller can re-read and re-apply.
        return fail("stale write: %s" % exc, path=str(path), out_path=str(out),
                    stale=True,
                    hint="another process wrote this file after it was read; retry the edit")
    res = {
        "ok": True,
        "path": str(path),
        "out_path": str(out),
        "in_place": in_place,
        "outputs": [str(out)],
        "mutated": [str(out)] if in_place else [],
        "changes": changes,
        "change_count": len(changes),
        "edited_spans": doc.edit_count,
        "bytes_before": len(before.encode("utf-8")),
        "bytes_after": written,
        "byte_identical": rendered == before,
    }
    if extra:
        res.update(extra)
    return res


def _find_symbols(doc, ref):
    return [s for s in _instances(doc) if _sym_ref(s) == ref]


def _prop_template(sym, doc):
    """(indent, anchor_node) for appending a new property to a symbol."""
    props = sym.children("property")
    if props:
        return doc.line_indent(props[0].start), props[-1]
    return doc.line_indent(sym.start) + "\t", None


def _set_one_field(doc, sym, field, value, changes, ref):
    """Set (or create) one property on one symbol node.

    Setting Reference also rewrites every `(reference …)` in `(instances …)`: netlisting
    reads the instance, the canvas reads the property, and a schematic where they disagree
    is annotated in the editor and unannotated in the netlist.
    """
    node = _prop_nodes(sym).get(field)
    if node is not None:
        args = node.atoms()
        if len(args) >= 2:
            old = str(args[1])
            if old != value:
                doc.replace_atom(args[1], value, quoted=True, label="%s.%s" % (ref, field))
                changes.append({"ref": ref, "field": field, "action": "updated",
                                "before": old, "after": value})
        else:                                      # (property "X") carrying no value
            doc.insert(node.kids[0].end, ' "%s"' % se.esc(value),
                       label="%s.%s" % (ref, field))
            changes.append({"ref": ref, "field": field, "action": "value_added",
                            "before": None, "after": value})
    else:
        indent, anchor = _prop_template(sym, doc)
        at = _at(sym) or {"x_mm": 0.0, "y_mm": 0.0}
        level = len(indent.replace("    ", "\t"))
        spec = ["property", se.Q(field), se.Q(value),
                ["at", at["x_mm"], at["y_mm"] or 0.0, 0],
                ["hide", "yes"],
                ["effects", ["font", ["size", 1.27, 1.27]]]]
        body = se.fmt(spec, level)
        if anchor is not None:
            doc.insert(anchor.end, "\n" + indent + body, label="%s.%s+" % (ref, field))
        else:
            doc.insert(sym.kids[-1].end if sym.kids else sym.start + 1,
                       "\n" + indent + body, label="%s.%s+" % (ref, field))
        changes.append({"ref": ref, "field": field, "action": "created",
                        "before": None, "after": value, "hidden": True})

    if field == "Reference":
        for inst in _instance_refs(sym):
            node = inst["_ref_node"]
            if node is None or not node.atoms():
                continue
            old = str(node.atoms()[0])
            if old != value:
                doc.replace_atom(node.atoms()[0], value, quoted=True,
                                 label="%s.instance" % ref)
                changes.append({"ref": ref, "field": "instances/reference",
                                "action": "updated", "before": old, "after": value,
                                "instance_path": inst["path"]})


def cmd_set_fields(req):
    """Set one or many fields on placed symbols. Each update is {ref, field, value}."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    updates = req.get("updates") or []
    if not updates:
        return fail("updates is required", hint='[{"ref": "R1", "field": "MPN", "value": "…"}]')
    changes, unknown = [], []
    seen_pairs = set()
    for upd in updates:
        ref = str(upd.get("ref", ""))
        field = str(upd.get("field", ""))
        value = upd.get("value")
        if not ref or not field:
            return fail("every update needs ref and field", bad_update=upd)
        if value is None:
            return fail("value must not be null (use \"\" to blank a field)", bad_update=upd)
        value = str(value)
        if (ref, field) in seen_pairs:
            return fail("two updates target %s.%s — the result would be ambiguous"
                        % (ref, field))
        seen_pairs.add((ref, field))
        syms = _find_symbols(doc, ref)
        if not syms:
            unknown.append(ref)
            continue
        # Multi-unit parts repeat the ref: every unit carries its own copy of the field.
        for sym in syms:
            _set_one_field(doc, sym, field, value, changes, ref)
    if unknown:
        return fail("no symbol(s) with reference %s in %s"
                    % (sorted(set(unknown)), path.name),
                    known_refs=sorted({_sym_ref(s) for s in _instances(doc) if _sym_ref(s)}),
                    hint="this tool edits ONE file; a hierarchical child lives in its own "
                         ".kicad_sch — call sch_list_sheets to find it")
    if not changes:
        return fail("nothing to do: every field already has the requested value",
                    path=str(path))
    return _finish(path, doc, req, changes)


def cmd_delete_symbol(req):
    """Delete every unit of one placed symbol (and only that symbol) from one file."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    ref = str(req["ref"])
    syms = _find_symbols(doc, ref)
    if not syms:
        return fail("no symbol with reference %r in %s" % (ref, path.name),
                    known_refs=sorted({_sym_ref(s) for s in _instances(doc) if _sym_ref(s)}))
    changes = []
    for sym in syms:
        doc.remove_node(sym, label="delete:%s" % ref)
        changes.append({"ref": ref, "action": "deleted", "lib_id": sym.scalar("lib_id"),
                        "unit": _symbol_summary(sym)["unit"], "at": _at(sym),
                        "uuid": _symbol_summary(sym)["uuid"]})
    extra = {"deleted_units": len(syms),
             "note": "wires, junctions and labels that touched it are left alone (they are "
                     "geometry, not children of the symbol); the lib_symbols cache entry is "
                     "kept, which is what eeschema does too"}
    return _finish(path, doc, req, changes, extra)


# ---------------------------------------------------------------------------
# geometry writers
# ---------------------------------------------------------------------------

def _append_root(doc, spec, label):
    """Append a new top-level element just before the closing paren of the document.

    Placement matters less than it looks: the schematic parser is order-insensitive for
    these elements, but eeschema rewrites the file grouped and sorted on its next save.
    New elements go after the last existing sibling of the same kind when there is one, so
    the file stays readable until then.
    """
    head = spec[0]
    siblings = doc.root.children(head)
    body = se.fmt(spec, 1)
    if siblings:
        indent = doc.line_indent(siblings[-1].start) or "\t"
        doc.insert(siblings[-1].end, "\n" + indent + body, label=label)
    else:
        # before the trailing (sheet_instances …)/(embedded_fonts …) block if we can, else
        # immediately before the document's own closing paren
        anchor = None
        for tail in ("sheet_instances", "embedded_fonts"):
            node = doc.root.child(tail)
            if node is not None:
                anchor = node.start if anchor is None else min(anchor, node.start)
        if anchor is not None:
            doc.insert(anchor, body + "\n\t", label=label)
        else:
            doc.insert(doc.root.end - 1, "\t" + body + "\n", label=label)
    return body


def cmd_add_wire(req):
    """Add a wire (or bus) segment between two points, in millimetres."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    x1 = _mm(req["x1_mm"], "x1_mm")
    y1 = _mm(req["y1_mm"], "y1_mm")
    x2 = _mm(req["x2_mm"], "x2_mm")
    y2 = _mm(req["y2_mm"], "y2_mm")
    if (x1, y1) == (x2, y2):
        return fail("refusing a zero-length wire")
    kind = str(req.get("kind") or "wire")
    if kind not in ("wire", "bus"):
        return fail("kind must be wire or bus", got=kind)
    style = str(req.get("style") or "solid")
    if style not in LINE_STYLES:
        return fail("unknown line style %r" % style, supported=list(LINE_STYLES))
    width = _num(req.get("width_mm"))
    uid = str(req.get("uuid") or _new_uuid())
    spec = [kind,
            ["pts", ["xy", x1, y1], ["xy", x2, y2]],
            ["stroke", ["width", width if width is not None else 0], ["type", style]],
            ["uuid", se.Q(uid)]]
    _append_root(doc, spec, "add_%s" % kind)
    changes = [{"action": "added", "element": kind, "from": [x1, y1], "to": [x2, y2],
                "uuid": uid, "style": style}]
    extra = {"uuid": uid, "diagonal": x1 != x2 and y1 != y2,
             "note": "a wire only connects where its ENDPOINT lands on a pin, a label or "
                     "another wire's endpoint (or a junction); crossing wires do not connect"}
    return _finish(path, doc, req, changes, extra)


def cmd_add_label(req):
    """Add a local, global or hierarchical label at a point."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    text = str(req.get("text") or "")
    if not text.strip():
        return fail("label text is required")
    kind = str(req.get("kind") or "local").lower()
    if kind not in LABEL_HEADS:
        return fail("kind must be local, global or hier", supported=sorted(LABEL_HEADS))
    head = LABEL_HEADS[kind]
    x = _mm(req["x_mm"], "x_mm")
    y = _mm(req["y_mm"], "y_mm")
    rot = _num(req.get("rotation")) or 0.0
    if rot not in (0.0, 90.0, 180.0, 270.0):
        return fail("rotation must be 0, 90, 180 or 270", got=rot)
    shape = req.get("shape")
    if head == "label" and shape:
        return fail("a local label has no shape (only global/hier labels do)")
    if head != "label":
        shape = str(shape or "input")
        if shape not in LABEL_SHAPES:
            return fail("unknown shape %r" % shape, supported=list(LABEL_SHAPES))
    size = _num(req.get("size_mm")) or 1.27
    uid = str(req.get("uuid") or _new_uuid())
    justify = "right" if rot == 180.0 else "left"
    spec = [head, se.Q(text)]
    if head != "label":
        spec.append(["shape", shape])
    spec.append(["at", x, y, rot])
    spec.append(["effects", ["font", ["size", size, size]], ["justify", justify]])
    spec.append(["uuid", se.Q(uid)])
    _append_root(doc, spec, "add_%s" % head)
    changes = [{"action": "added", "element": head, "text": text, "at": [x, y, rot],
                "shape": shape if head != "label" else None, "uuid": uid}]
    extra = {"uuid": uid, "element": head,
             "note": "a label names the net only where it TOUCHES a wire or pin; a label in "
                     "empty space is an ERC 'unconnected' item, not a net"}
    return _finish(path, doc, req, changes, extra)


def cmd_add_no_connect(req):
    """Mark a pin deliberately unconnected (the X flag ERC reads)."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    x = _mm(req["x_mm"], "x_mm")
    y = _mm(req["y_mm"], "y_mm")
    uid = str(req.get("uuid") or _new_uuid())
    for nc in doc.root.children("no_connect"):
        vals = nc.floats("at") or []
        if len(vals) >= 2 and abs(vals[0] - x) < 1e-6 and abs(vals[1] - y) < 1e-6:
            return fail("a no_connect is already at (%s, %s)" % (x, y),
                        existing_uuid=(str(nc.child("uuid").atoms()[0])
                                       if nc.child("uuid") is not None else None))
    _append_root(doc, ["no_connect", ["at", x, y], ["uuid", se.Q(uid)]], "add_no_connect")
    changes = [{"action": "added", "element": "no_connect", "at": [x, y], "uuid": uid}]
    extra = {"uuid": uid,
             "note": "the flag must sit exactly on the pin's connection point or ERC still "
                     "reports the pin as unconnected AND the flag as unconnected"}
    return _finish(path, doc, req, changes, extra)


# ---------------------------------------------------------------------------
# place_symbol — the one writer that needs a library definition
# ---------------------------------------------------------------------------

def _instance_template(doc):
    """(project_name, sheet_path) copied from a symbol already in this file.

    A placement's `(instances (project "NAME" (path "/UUID/UUID" …)))` is what netlisting
    and the board read.  For a CHILD sheet the path is /root-uuid/sheet-uuid — values this
    file does not contain, since a child does not know who instantiated it.  Copying the
    block from a sibling symbol is therefore the only way to place into a child sheet
    correctly; a file with no symbols at all falls back to the root form.
    """
    for sym in _instances(doc):
        for inst in _instance_refs(sym):
            if inst["path"] and inst["project"] is not None:
                return inst["project"], inst["path"]
    uid = doc.root.child("uuid")
    root_uuid = str(uid.atoms()[0]) if uid is not None and uid.atoms() else _new_uuid()
    return None, "/" + root_uuid


def _reindent(text, from_indent, to_indent):
    """Shift every line but the first from one indent prefix to another.

    A definition lifted out of a .kicad_sym sits one tab deep; pasted into a schematic's
    `(lib_symbols …)` it belongs two tabs deep.  Without this the block parses but reads as
    if it were mis-nested, which is exactly the kind of noise this module exists to avoid.
    """
    if from_indent == to_indent:
        return text
    lines = text.split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if line.startswith(from_indent):
            out.append(to_indent + line[len(from_indent):])
        else:
            out.append(line)
    return "\n".join(out)


def _copy_definition(doc, defn_text, lib_id, changes, src_indent="\t", indent="\t\t"):
    """Splice a symbol definition into `(lib_symbols …)` under the placement's lib_id."""
    block = doc.root.child("lib_symbols")
    body = re.sub(r'^\(symbol\s+"[^"]*"', '(symbol "%s"' % se.esc(lib_id), defn_text, count=1)
    body = _reindent(body, src_indent, indent)
    if block is None:
        anchor = None
        for tail in ("sheet_instances", "embedded_fonts"):
            node = doc.root.child(tail)
            if node is not None:
                anchor = node.start if anchor is None else min(anchor, node.start)
        text = "(lib_symbols\n" + indent + body + "\n\t)\n\t"
        doc.insert(anchor if anchor is not None else doc.root.end - 1, text,
                   label="lib_symbols+")
    else:
        kids = block.children("symbol")
        if kids:
            doc.insert(kids[-1].end, "\n" + (doc.line_indent(kids[-1].start) or indent) + body,
                       label="lib_symbols+%s" % lib_id)
        else:
            doc.insert(block.end - 1, indent + body + "\n\t", label="lib_symbols+%s" % lib_id)
    changes.append({"action": "cached_definition", "lib_id": lib_id,
                    "bytes": len(body)})


def _definition_from_library(lib_path, name):
    """(source_text, node, library_path, source_indent) for one symbol in a .kicad_sym."""
    p, doc = _load(lib_path, SYM_SUFFIX)
    found = None
    for sym in doc.root.children("symbol"):
        args = sym.atoms()
        if args and str(args[0]) == name:
            found = sym
            break
    if found is None:
        names = [str(s.atoms()[0]) for s in doc.root.children("symbol") if s.atoms()]
        raise ValueError("no symbol %r in %s (has %d symbols)" % (name, p.name, len(names)))
    if found.scalar("extends"):
        raise ValueError("symbol %r extends %r — resolve the parent first; this writer does "
                         "not flatten inheritance" % (name, found.scalar("extends")))
    return found.text(doc.text), found, p, doc.line_indent(found.start) or "\t"


def cmd_place_symbol(req):
    """Place one symbol instance, caching its definition when the file lacks one."""
    path, doc = _load(req["path"], SCH_SUFFIX)
    lib_id = str(req["lib_id"])
    ref = str(req["ref"])
    value = str(req.get("value") or "")
    x = _mm(req["x_mm"], "x_mm")
    y = _mm(req["y_mm"], "y_mm")
    rot = _num(req.get("rotation")) or 0.0
    if rot not in (0.0, 90.0, 180.0, 270.0):
        return fail("rotation must be 0, 90, 180 or 270", got=rot)
    unit = int(req.get("unit") or 1)
    if ":" not in lib_id:
        return fail("lib_id must be LIBRARY:SYMBOL", got=lib_id)
    if _find_symbols(doc, ref):
        return fail("reference %r is already used in %s" % (ref, path.name),
                    hint="pick an unused refdes, or use ref='%s?' and let eeschema annotate"
                         % re.sub(r"\d+$", "", ref))

    changes = []
    defs = _lib_symbols(doc)
    defn = defs.get(lib_id)
    from_lib = req.get("from_lib")
    if defn is None:
        if not from_lib:
            return fail("lib_id %r is not in this file's lib_symbols cache" % lib_id,
                        cached_lib_ids=sorted(defs),
                        hint="pass from_lib=<a .kicad_sym file> so the definition can be "
                             "copied into the cache; a placement whose definition is missing "
                             "loads as a broken symbol")
        try:
            defn_text, defn, lib_file, src_indent = _definition_from_library(
                from_lib, lib_id.split(":", 1)[1])
        except ValueError as exc:
            return fail(str(exc))
        block = doc.root.child("lib_symbols")
        cached = block.children("symbol") if block is not None else []
        target_indent = doc.line_indent(cached[0].start) if cached else "\t\t"
        _copy_definition(doc, defn_text, lib_id, changes, src_indent, target_indent or "\t\t")
        changes.append({"action": "sourced", "from_lib": str(lib_file)})

    detail = _definition_detail(defn, lib_id)
    if unit < 1 or (detail["unit_count"] and unit > detail["unit_count"]):
        return fail("unit %d out of range (definition has %d)" % (unit, detail["unit_count"]))
    pin_numbers = _unit_pin_numbers(defn, unit)
    if not value:
        value = detail["properties"].get("Value") or lib_id.split(":", 1)[1]
    footprint = req.get("footprint")
    if footprint is None:
        footprint = detail["properties"].get("Footprint") or ""

    project, sheet_path = _instance_template(doc)
    sym_uuid = str(req.get("uuid") or _new_uuid())

    def prop(name, val, dy, hide):
        spec = ["property", se.Q(name), se.Q(val), ["at", x, y + dy, 0]]
        if hide:
            spec.append(["hide", "yes"])
        spec.append(["effects", ["font", ["size", 1.27, 1.27]]])
        return spec

    spec = ["symbol",
            ["lib_id", se.Q(lib_id)],
            ["at", x, y, rot],
            ["unit", unit],
            ["body_style", 1],
            ["exclude_from_sim", "no"],
            ["in_bom", "yes"],
            ["on_board", "yes"],
            ["in_pos_files", "yes"],
            ["dnp", "no"],
            ["uuid", se.Q(sym_uuid)],
            prop("Reference", ref, -5.08, False),
            prop("Value", value, 5.08, False),
            prop("Footprint", str(footprint), 0.0, True),
            prop("Datasheet", detail["properties"].get("Datasheet") or "", 0.0, True),
            prop("Description", detail["properties"].get("Description") or "", 0.0, True)]
    for number in pin_numbers:
        spec.append(["pin", se.Q(number), ["uuid", se.Q(_new_uuid())]])
    path_spec = ["path", se.Q(sheet_path), ["reference", se.Q(ref)], ["unit", unit]]
    project_spec = ["project", se.Q(project if project is not None else path.stem), path_spec]
    spec.append(["instances", project_spec])

    _append_root(doc, spec, "place:%s" % ref)
    changes.append({"action": "placed", "ref": ref, "lib_id": lib_id, "value": value,
                    "footprint": str(footprint), "at": [x, y, rot], "unit": unit,
                    "uuid": sym_uuid, "pins": len(pin_numbers)})
    extra = {"uuid": sym_uuid, "pin_count": len(pin_numbers),
             "instance_project": project if project is not None else path.stem,
             "instance_path": sheet_path,
             "definition_was_cached": from_lib is None or lib_id in defs,
             "note": "the symbol is placed but WIRED TO NOTHING; its pins are unconnected "
                     "until add_wire/add_label reaches them, which ERC will report"}
    if detail["unit_count"] > 1:
        extra["multi_unit_note"] = ("this part has %d units — only unit %d was placed; the "
                                    "others need their own placements sharing the refdes"
                                    % (detail["unit_count"], unit))
    return _finish(path, doc, req, changes, extra)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

COMMANDS = {
    "list_symbols": cmd_list_symbols,
    "get_symbol": cmd_get_symbol,
    "annotate_check": cmd_annotate_check,
    "list_sheets": cmd_list_sheets,
    "find": cmd_find,
    "sym_lib_list": cmd_sym_lib_list,
    "sym_lib_get": cmd_sym_lib_get,
    "parse_netlist": cmd_parse_netlist,
    "set_fields": cmd_set_fields,
    "delete_symbol": cmd_delete_symbol,
    "add_wire": cmd_add_wire,
    "add_label": cmd_add_label,
    "add_no_connect": cmd_add_no_connect,
    "place_symbol": cmd_place_symbol,
}

#: Commands that write a file (the server needs to know before it calls one).
WRITERS = ("set_fields", "delete_symbol", "add_wire", "add_label", "add_no_connect",
           "place_symbol")


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        emit({"ok": True, "commands": sorted(COMMANDS), "writers": list(WRITERS),
              "usage": "sch_worker.py <command> '<json payload>'"}, code=0)
    command = argv[1]
    fn = COMMANDS.get(command)
    if fn is None:
        emit(fail("unknown command %r" % command, commands=sorted(COMMANDS)))
    raw = argv[2] if len(argv) > 2 else "{}"
    try:
        req = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        emit(fail("payload is not valid JSON: %s" % exc))
    if not isinstance(req, dict):
        emit(fail("payload must be a JSON object"))
    try:
        result = fn(req)
    except KeyError as exc:
        result = fail("missing required key: %s" % exc)
    except (ValueError, OSError) as exc:
        result = fail("%s: %s" % (type(exc).__name__, exc))
    except Exception as exc:                                  # never leak a traceback
        import traceback
        result = fail("%s: %s" % (type(exc).__name__, exc),
                      traceback=traceback.format_exc()[-3000:])
    result.setdefault("command", command)
    emit(result)


if __name__ == "__main__":
    main(sys.argv)
