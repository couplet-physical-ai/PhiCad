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
"""Symbol libraries as *data*: pins, a bounding box, and a re-embeddable body.

This is `library.py`'s job done for schematic symbols: parse the stock `.kicad_sym`
libraries once with `phicad/pylib/sexp.py`, answer "what pins does Device:R have" from the
parse tree, and hand back the symbol's own bytes for embedding in a schematic's
`lib_symbols` block.

The verbatim-body discipline, and its one exception
---------------------------------------------------
A schematic's `lib_symbols` block is written by the same serializer that writes a
`.kicad_sym` file (`sch_io_kicad_sexpr_lib_cache.cpp:366`), so a library symbol's text can
be copied into a schematic byte for byte — the only edit the format needs is the outer
name, `(symbol "R"` -> `(symbol "Device:R"`; the inner unit sub-symbols (`R_0_1`) keep
their bare names.  Copying bytes cannot lose a construct the parser here does not model,
which is the same reason `library.py` and `emit.py` never re-serialize.

The exception is a **derived** symbol (`(extends "PARENT")` — 12,318 of the ~22,860 stock
symbols).  A derived block carries no pins or graphics of its own; eeschema flattens it
against its parent on placement (`sch_symbol.cpp:536` -> `lib_symbol.cpp:590`), and no
schematic ever embeds one unflattened.  Embedded verbatim, the file still loads and ERC
shows only warnings — but the part's pins are silently absent from the exported netlist,
which is the worst failure a generator can ship.  So `resolve()` always returns a
flattened symbol, built by splicing the derived symbol's `(property …)` overrides into the
parent's verbatim text — mirroring exactly what `LIB_SYMBOL::Flatten()` overrides (name,
mandatory fields when non-empty, user fields, which carry ki_keywords/ki_fp_filters too).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_PYLIB = Path(__file__).resolve().parent.parent / "pylib"
if str(_PYLIB) not in sys.path:
    sys.path.insert(0, str(_PYLIB))

import sexp  # noqa: E402

# The fork's stock symbol set, the same install the global sym-lib-table points at via
# ${KICAD10_SYMBOL_DIR}.  Overridable because the footprint side's hard-coded path is
# already a documented limitation; no reason to repeat it here.
STOCK_DIR = Path(os.environ.get(
    "PHICAD_SYMBOL_DIR",
    os.path.expanduser("~/Library/Application Support/kicad/stock/symbols")))

# Mandatory fields, in LIB_SYMBOL::Flatten() terms: a derived symbol's value overrides the
# parent's only when non-empty.  Every other property always overrides.
_MANDATORY_FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "Description")


class SymbolError(RuntimeError):
    """A symbol could not be found, read, or understood."""


@dataclass
class SymbolPin:
    """One pin, in symbol (Y-up) coordinates.  `x, y` is the connection point."""

    number: str
    name: str
    etype: str                      # passive | power_in | power_out | input | output | …
    x: float
    y: float
    length: float = 2.54
    angle: float = 0.0              # direction from the connection point toward the body
    hide: bool = False


@dataclass
class Symbol:
    """A parsed (and, when derived, flattened) library symbol."""

    lib: str
    name: str
    pins: Dict[str, SymbolPin] = field(default_factory=dict)
    extends: str = ""               # the parent it was flattened from, for reporting
    body: str = ""                  # flattened verbatim block, outer name still bare
    prefix: str = "U"               # the Reference property's value ("R", "C", "J", …)

    @property
    def libid(self) -> str:
        return f"{self.lib}:{self.name}"

    def power_in_pins(self) -> List[str]:
        return sorted(n for n, p in self.pins.items() if p.etype == "power_in")

    def embedded(self) -> str:
        """The body renamed for a schematic's `lib_symbols` block.

        The one legal edit: `(symbol "R"` becomes `(symbol "Device:R"`.  Inner unit
        sub-symbols keep their bare names — the loader strips the nickname itself when it
        parses unit names (`sch_io_kicad_sexpr_lib_cache.cpp:500`).
        """
        old = f'(symbol "{_escape(self.name)}"'
        new = f'(symbol "{_escape(self.libid)}"'
        assert self.body.lstrip().startswith(old)
        return self.body.replace(old, new, 1)

    def bbox(self) -> Tuple[float, float, float, float]:
        """Graphics + pin extent in symbol coordinates — for layout spacing only.

        Deliberately loose (arc/bezier control points instead of swept outlines): the
        box exists to keep neighbouring symbols and their labels apart on a sheet.
        """
        xs: List[float] = [0.0]
        ys: List[float] = [0.0]
        doc = sexp.parse(self.body)
        for unit in sexp.children(doc, "symbol"):
            for node in unit:
                if not isinstance(node, list):
                    continue
                kind = sexp.head(node)
                if kind == "pin":
                    at = sexp.floats(node, "at") or [0.0, 0.0]
                    xs.append(at[0])
                    ys.append(at[1])
                elif kind in ("rectangle", "arc", "bezier", "polyline", "circle", "text"):
                    for key in ("start", "mid", "end", "center", "at"):
                        pt = sexp.floats(node, key)
                        if pt and len(pt) >= 2:
                            xs.append(pt[0])
                            ys.append(pt[1])
                    pts = sexp.child(node, "pts")
                    if pts is not None:
                        for xy in sexp.children(pts, "xy"):
                            vals = sexp.atoms(xy)
                            if len(vals) >= 2:
                                xs.append(float(vals[0]))
                                ys.append(float(vals[1]))
        return (min(xs), min(ys), max(xs), max(ys))


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------------------
# text scanning — paren-balanced slices that respect quoted strings
# ---------------------------------------------------------------------------------------

def _block_end(text: str, start: int) -> int:
    """Index one past the ')' closing the '(' at `start`.

    A real scanner, not a paren counter: symbol descriptions legitimately contain
    parentheses inside quoted strings, and counting those corrupts the slice.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise SymbolError(f"unbalanced s-expression at offset {start}")


def _find_named_blocks(text: str, head: str) -> List[Tuple[str, int, int]]:
    """Every `({head} "NAME"` block in `text` as (name, start, end)."""
    out: List[Tuple[str, int, int]] = []
    pattern = re.compile(r"\(" + head + r'\s+"((?:[^"\\]|\\.)*)"')
    pos = 0
    while True:
        m = pattern.search(text, pos)
        if m is None:
            return out
        end = _block_end(text, m.start())
        out.append((m.group(1).replace('\\"', '"').replace("\\\\", "\\"),
                    m.start(), end))
        pos = end


# ---------------------------------------------------------------------------------------
# parsing one symbol block
# ---------------------------------------------------------------------------------------

def _parse_pins(tree) -> Dict[str, SymbolPin]:
    pins: Dict[str, SymbolPin] = {}
    for unit in sexp.children(tree, "symbol"):
        for node in sexp.children(unit, "pin"):
            args = sexp.atoms(node)
            etype = str(args[0]) if args else "passive"
            at = sexp.floats(node, "at") or [0.0, 0.0]
            length = sexp.floats(node, "length")
            name_node = sexp.child(node, "name")
            number_node = sexp.child(node, "number")
            number = str(sexp.atoms(number_node)[0]) if number_node is not None else ""
            name = str(sexp.atoms(name_node)[0]) if name_node is not None else ""
            hide_node = sexp.child(node, "hide")
            hide = bool(hide_node is not None
                        and (not sexp.atoms(hide_node)
                             or str(sexp.atoms(hide_node)[0]) == "yes"))
            # Duplicate numbers are legal (jumpered pins); the first wins for geometry,
            # matching library.py's pad policy.
            pins.setdefault(number, SymbolPin(
                number=number, name=name, etype=etype,
                x=at[0], y=at[1],
                length=(length[0] if length else 2.54),
                angle=(at[2] if len(at) > 2 else 0.0),
                hide=hide))
    return pins


def _properties(block: str) -> List[Tuple[str, str]]:
    """Top-level `(property "Name" …)` blocks of one symbol as (name, verbatim text)."""
    # Only depth-1 properties: unit sub-symbols carry none, but pins carry (name …)
    # nodes a regex could confuse; the named-block scanner keyed on `property` is safe
    # because that head never appears deeper inside a symbol.
    return [(name, block[s:e]) for name, s, e in _find_named_blocks(block, "property")]


def _property_value(block: str, name: str) -> str:
    for pname, text in _properties(block):
        if pname == name:
            m = re.match(r'\(property\s+"(?:[^"\\]|\\.)*"\s+"((?:[^"\\]|\\.)*)"', text)
            if m:
                return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return ""


def _flatten_text(parent_body: str, parent_name: str, child_block: str,
                  child_name: str) -> str:
    """LIB_SYMBOL::Flatten() as a text splice.

    Start from the parent's (already flattened) body and apply what Flatten() applies:
    the name — outer and the unit sub-symbol prefixes — and the derived symbol's
    property overrides (mandatory fields only when non-empty; everything else always,
    which is how ki_keywords and ki_fp_filters travel).  Attributes like in_bom and the
    pin_names offset stay the parent's, as they do in eeschema.
    """
    body = parent_body
    body = body.replace(f'(symbol "{_escape(parent_name)}"',
                        f'(symbol "{_escape(child_name)}"', 1)
    body = re.sub(r'\(symbol "' + re.escape(_escape(parent_name)) + r'_(\d+_\d+)"',
                  lambda m: f'(symbol "{_escape(child_name)}_{m.group(1)}"', body)

    overrides: List[Tuple[str, str]] = []
    for name, text in _properties(child_block):
        if name in _MANDATORY_FIELDS and not _property_value(child_block, name):
            continue
        overrides.append((name, text))

    parent_props = {name: (s, e)
                    for name, s, e in _find_named_blocks(body, "property")}
    append: List[str] = []
    for name, text in overrides:
        if name in parent_props:
            s, e = parent_props[name]
            body = body[:s] + text + body[e:]
            # Offsets shift after a splice; rescan for the properties still pending.
            parent_props = {n: (s2, e2)
                            for n, s2, e2 in _find_named_blocks(body, "property")}
        else:
            append.append(text)

    if append:
        # New user fields go where the serializer puts them: after the existing
        # properties, before the first unit sub-symbol.
        m = re.search(r'\n(\s*)\(symbol "', body)
        insert_at = m.start() if m else body.rfind(")")
        indent = m.group(1) if m else "\t\t"
        body = (body[:insert_at]
                + "".join(f"\n{indent}{text}" for text in append)
                + body[insert_at:])
    return body


# ---------------------------------------------------------------------------------------
# the library index
# ---------------------------------------------------------------------------------------

class SymbolLibrary:
    """Resolves `Lib:Name` against roots holding `*.kicad_sym` files."""

    def __init__(self, roots: Iterable[Path]):
        self.roots: List[Path] = [Path(r) for r in roots]
        # lib name -> {symbol name: verbatim block}; whole files are indexed at once
        # because one design usually pulls several symbols from the same library.
        self._blocks: Dict[str, Dict[str, str]] = {}
        self._cache: Dict[str, Symbol] = {}

    @classmethod
    def default(cls, extra: Optional[Iterable[Path]] = None) -> "SymbolLibrary":
        roots = [Path(p) for p in (extra or [])]
        roots.append(STOCK_DIR)
        return cls(roots)

    def _index(self, lib: str) -> Dict[str, str]:
        if lib in self._blocks:
            return self._blocks[lib]
        for root in self.roots:
            path = root / f"{lib}.kicad_sym"
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise SymbolError(f"cannot read {path}: {exc}") from None
                scanner = _DepthScanner(text)
                blocks = {name: text[s:e]
                          for name, s, e in _find_named_blocks(text, "symbol")
                          # Unit sub-symbols match the scanner too; only top-level
                          # symbols lack the _<unit>_<style> suffix... except symbols
                          # whose real names end that way don't exist in practice, and
                          # depth would be the robust test — so use depth via indent:
                          # top-level blocks in a .kicad_sym start at column 1 (one tab).
                          if scanner.is_top_level(s)}
                self._blocks[lib] = blocks
                return blocks
        searched = "\n  ".join(str(r / f"{lib}.kicad_sym") for r in self.roots)
        raise SymbolError(f"symbol library {lib!r} not found. Searched:\n  {searched}")

    def resolve(self, libid: str, _chain: Optional[List[str]] = None) -> Symbol:
        """`"Device:R"` -> a flattened Symbol (cached)."""
        if libid in self._cache:
            return self._cache[libid]
        if ":" not in libid:
            raise SymbolError(f"symbol id {libid!r} must be 'Library:Name' "
                              "(e.g. 'Device:R')")
        lib, name = libid.split(":", 1)
        blocks = self._index(lib)
        if name not in blocks:
            raise SymbolError(f"symbol {libid} not found; the library has "
                              f"{len(blocks)} symbols")
        block = blocks[name]
        tree = sexp.parse(block)
        extends = sexp.scalar(tree, "extends") or ""

        if extends:
            # Parents live in the same library file.  Refusing a cycle beats recursing
            # forever on a corrupt library.
            chain = list(_chain or [])
            if libid in chain:
                raise SymbolError(f"circular (extends …) chain: "
                                  + " -> ".join(chain + [libid]))
            parent = self.resolve(f"{lib}:{extends}", chain + [libid])
            body = _flatten_text(parent.body, parent.name, block, name)
            pins = dict(parent.pins)
            prefix = _property_value(block, "Reference") or parent.prefix
        else:
            body = block
            pins = _parse_pins(tree)
            prefix = _property_value(block, "Reference") or "U"

        symbol = Symbol(lib=lib, name=name, pins=pins, extends=extends,
                        body=body, prefix=prefix)
        self._cache[libid] = symbol
        return symbol

    def has(self, libid: str) -> bool:
        try:
            self.resolve(libid)
        except SymbolError:
            return False
        return True

    def suggest(self, fragment: str, limit: int = 12) -> List[str]:
        """Symbol ids resembling `fragment` — the "did you mean" list.

        Same progressive-shortening policy as Library.suggest(): a typo is usually in
        the tail, and six characters is the floor below which matches become noise.
        """
        needle = fragment.strip().lower()
        while len(needle) >= 4:
            hits: List[str] = []
            for root in self.roots:
                if not root.is_dir():
                    continue
                for libfile in sorted(root.glob("*.kicad_sym")):
                    lib = libfile.stem
                    for name in sorted(self._index(lib)):
                        if needle in name.lower() or needle in lib.lower():
                            hits.append(f"{lib}:{name}")
                            if len(hits) >= limit:
                                return hits
            if hits:
                return hits
            needle = needle[:-1]
        return []


class _DepthScanner:
    """Paren depth at increasing offsets in one document, scanned once.

    `_is_top_level` answers the same question correctly but restarts from offset 0 every
    call, which makes indexing a library quadratic in its size: the stock `Device` library
    took 66 s and `Connector_Generic` 99 s, so a full `synth selftest` looked like a hang.

    `_find_named_blocks` already yields blocks in increasing order, so the scan can simply
    resume from where it stopped. Same quote-awareness, same answer, one pass.
    """

    __slots__ = ("text", "i", "depth")

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.depth = 0

    def depth_at(self, start: int) -> int:
        text = self.text
        i, depth = self.i, self.depth
        if start < i:                       # out of order: fall back to a fresh scan
            i, depth = 0, 0
        n = min(start, len(text))
        while i < n:
            c = text[i]
            if c == '"':
                i += 1
                while i < n:
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == '"':
                        break
                    i += 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        self.i, self.depth = i, depth
        return depth

    def is_top_level(self, start: int) -> bool:
        return self.depth_at(start) == 1


def _is_top_level(text: str, start: int) -> bool:
    """True when the block starting at `start` is a direct child of the library node.

    Measured by real paren DEPTH, not by indentation.  The first version of this compared
    the line's leading whitespace against a single tab, which is exactly what KiCad's own
    serializer emits -- and silently rejected every symbol in a library written by anything
    else.  `easyeda2kicad` indents with two SPACES, so a fetched parts-pipeline library
    indexed as "the library has 0 symbols" while the file was perfectly valid, and the
    parts -> synth path could not use a single fetched symbol.  Depth is the property the
    format actually guarantees: a top-level `(symbol "NAME"` sits at depth 1 inside
    `(kicad_symbol_lib`, and a unit sub-symbol at depth 2, whatever the whitespace.

    Counting is quote-aware for the same reason `_block_end` is: a property value such as
    `"100nF (104) +/-10% 50V"` contains parens that are text, not structure.
    """
    depth = 0
    i = 0
    n = min(start, len(text))
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    # depth 1 == inside (kicad_symbol_lib and nothing else.
    return depth == 1


if __name__ == "__main__":
    # Smoke test:  python3 phicad/synth/symbols.py Device:R Regulator_Linear:AP2112K-3.3
    ids = sys.argv[1:] or ["Device:R", "Sensor:BME280",
                           "Regulator_Linear:AP2112K-3.3",
                           "Connector_Generic:Conn_01x04"]
    library = SymbolLibrary.default()
    for libid in ids:
        s = library.resolve(libid)
        flat = f" (extends {s.extends})" if s.extends else ""
        print(f"{s.libid}{flat}  prefix={s.prefix}  pins={len(s.pins)}  "
              f"bbox={tuple(round(v, 2) for v in s.bbox())}")
        for number in sorted(s.pins, key=lambda n: (len(n), n)):
            p = s.pins[number]
            print(f"    pin {number:>3}  {p.name!r:<12} {p.etype:<10} "
                  f"at ({p.x:+.2f}, {p.y:+.2f}) angle {p.angle:g}")
