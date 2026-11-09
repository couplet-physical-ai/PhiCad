#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
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
"""Minimal read-only s-expression parser for KiCad files. Stdlib only.

Written because third-party parsers (kiutils 1.4.8) hard-fail on this fork's
board format (version 20260831): the fork dropped the numeric net table and
emits name-only net references — `(net "GND")` where stable KiCad writes
`(net 42 "GND")` at declaration and `(net 42)` at reference sites. This parser
makes no schema assumptions at all: a document is nested lists of Atom.

    node        -> list  [head, child, …]      e.g. ['net', Atom('GND', quoted=True)]
    atom        -> Atom (str subclass), .quoted tells "42" from 42 and "" from nothing

Helpers cover the common access patterns (children by head, scalar args,
footprint pads/properties) for BOTH stable and fork formats. Parsing a 1 MB
board takes well under a second; nothing here can write files — splicing
stays in merge_routes.py, which never reserializes.
"""

from __future__ import annotations


class Atom(str):
    """A bare or quoted token. `.quoted` distinguishes (net "0") from (net 0)."""
    quoted: bool

    def __new__(cls, value: str, quoted: bool = False) -> "Atom":
        obj = super().__new__(cls, value)
        obj.quoted = quoted
        return obj


def parse(text: str):
    """Parse one s-expression document -> nested lists of Atom."""
    i, n = 0, len(text)
    stack: list[list] = []
    root = None
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "(":
            node: list = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            if not stack:
                raise ValueError(f"unbalanced ')' at offset {i}")
            node = stack.pop()
            if not stack:
                if root is not None:
                    raise ValueError("multiple top-level expressions")
                root = node
            i += 1
        elif c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if ch == '"':
                    break
                buf.append(ch)
                j += 1
            if j >= n:
                raise ValueError(f"unterminated string at offset {i}")
            if not stack:
                raise ValueError(f"stray string at offset {i}")
            stack[-1].append(Atom("".join(buf), quoted=True))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            if not stack:
                raise ValueError(f"stray atom at offset {i}")
            stack[-1].append(Atom(text[i:j], quoted=False))
            i = j
    if stack:
        raise ValueError("unbalanced '(' — document truncated?")
    if root is None:
        raise ValueError("empty document")
    return root


def parse_file(path) -> list:
    with open(path, encoding="utf-8") as fh:
        return parse(fh.read())


# ---------------------------------------------------------------------------
# access helpers
# ---------------------------------------------------------------------------

def head(node) -> str | None:
    """First atom of a list node, else None."""
    if isinstance(node, list) and node and isinstance(node[0], Atom):
        return str(node[0])
    return None


def _reject_atom(node, func: str):
    """Guard the one mistake that fails SILENTLY instead of loudly.

    `parse()` returns the root node itself, so a caller who writes
    `parse_file(p)[0]` gets the head Atom rather than a document.  An Atom is a
    str subclass, so iterating it yields characters, every `isinstance(c, list)`
    test fails, and the caller gets an empty result with no error -- a gate
    written that way reports "0 problems" while measuring nothing at all.
    A str can never contain child nodes, so refusing one breaks no valid use.
    """
    if isinstance(node, str):
        raise TypeError(
            "sexp.%s() needs a list node, got a bare atom %r -- parse()/parse_file() "
            "already return the ROOT node, so do not index it ([0] yields this head "
            "atom)" % (func, str(node)[:40]))


def children(node, name: str):
    """All direct child lists with the given head."""
    _reject_atom(node, "children")
    return [c for c in node if isinstance(c, list) and head(c) == name]


def child(node, name: str):
    """First direct child list with the given head, or None."""
    _reject_atom(node, "child")
    for c in node:
        if isinstance(c, list) and head(c) == name:
            return c
    return None


def atoms(node) -> list[Atom]:
    """Non-list arguments of a node (excluding the head)."""
    _reject_atom(node, "atoms")
    return [c for c in node[1:] if isinstance(c, Atom)]


def scalar(node, name: str, idx: int = 0):
    """atoms(child(node, name))[idx] as str, or None."""
    c = child(node, name)
    if c is None:
        return None
    a = atoms(c)
    return str(a[idx]) if len(a) > idx else None


def floats(node, name: str) -> list[float] | None:
    """Numeric arguments of a named child, e.g. (at 12.3 4.5 90) -> [12.3, 4.5, 90]."""
    c = child(node, name)
    if c is None:
        return None
    out = []
    for a in atoms(c):
        try:
            out.append(float(a))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------------------
# board-level helpers (stable + fork formats)
# ---------------------------------------------------------------------------

def net_of(node) -> str | None:
    """Net NAME referenced by a pad/segment/via/arc/zone node, both formats.

    stable: (net 42 "GND") at declaration, (net 42) at reference —
            resolve references through net_table() when you need names.
    fork:   (net "GND") everywhere.
    Returns an Atom — `.quoted` True means it is a NAME; False means it is a
    numeric reference to resolve via net_table(). None if no net child.
    """
    c = child(node, "net")
    if c is None:
        return None
    args = atoms(c)
    if not args:
        return None
    for a in args:
        if a.quoted:
            return a
    return args[0]   # numeric reference — resolve via net_table()


def net_table(doc) -> dict[str, str]:
    """Top-level (net N "NAME") declarations -> {number_str: name}. Empty for fork files."""
    table: dict[str, str] = {}
    for c in children(doc, "net"):
        args = atoms(c)
        if len(args) >= 2 and not args[0].quoted and args[1].quoted:
            table[str(args[0])] = str(args[1])
    return table


def fp_property(fp, key: str) -> str | None:
    """(property "Key" "Value" …) lookup, case-insensitive key."""
    for p in children(fp, "property"):
        args = atoms(p)
        if len(args) >= 2 and str(args[0]).lower() == key.lower():
            return str(args[1])
    # pre-v8 fallback: (fp_text reference|value "X" …)
    if key.lower() in ("reference", "value"):
        for t in children(fp, "fp_text"):
            args = atoms(t)
            if len(args) >= 2 and str(args[0]).lower() == key.lower():
                return str(args[1])
    return None


def fp_attrs(fp) -> set[str]:
    """Footprint (attr …) flags plus a synthetic 'dnp' from v7-style (dnp yes)."""
    flags: set[str] = set()
    a = child(fp, "attr")
    if a is not None:
        flags |= {str(x) for x in atoms(a)}
    d = child(fp, "dnp")
    if d is not None and (not atoms(d) or str(atoms(d)[0]) in ("yes", "true")):
        flags.add("dnp")
    return flags
