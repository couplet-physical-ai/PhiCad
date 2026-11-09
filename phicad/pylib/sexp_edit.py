#!/usr/bin/env python3
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
"""Span-preserving s-expression editor for KiCad files. Stdlib only, 3.9-safe.

WHY THIS EXISTS (measured, not assumed)
    Editing a .kicad_sch through a round-tripping library rewrites the whole file:

    kiutils 1.4.8   LOSES DATA on a KiCad 10 schematic (demos/pic_programmer):
                    do_not_autoplace 641->0, hide 391->0, exclude_from_sim 135->0,
                    in_pos_files 126->0, body_style 105->0, embedded_fonts 22->0,
                    duplicate_pin_numbers_are_jumpers 21->0, pin_numbers 6->0,
                    generator_version 1->0, plus italic/bold on text.  229174 -> 178091 bytes.
    kicad-skip 0.2.5 keeps every token (token histogram identical) but reflows the entire
                    file onto its own lines: 16830 diff lines against the original, so
                    `kicad-cli sch diff` and `git diff` become unreadable and unreviewable.

    This module instead PARSES the text into nodes that remember their character spans and
    then splices new text into those spans.  Every byte nobody touched stays exactly where
    it was: a one-field edit produces a one-line diff.

MODEL
    Doc(text)          parse once; `root` is a Node tree over the ORIGINAL text
    Node               .head, .kids (Node|Atom, kids[0] is the head atom), .start/.end
    Atom(str)          .quoted (tells `"42"` from `42`), .start/.end
    Doc.replace/insert/remove_node   queue edits (nothing mutates the text)
    Doc.render()/save()              apply queued edits, rightmost-first, overlap-checked

WRITING NEW NODES
    `fmt()` renders nested Python lists in KiCad's own layout (tab indent, atoms on the head
    line, `(xy …)` runs kept inline) so appended elements are indistinguishable from what
    eeschema writes.  Quote a string atom with Q("…"); bare tokens are plain str.

NOT A SCHEMA
    Nothing here knows what a symbol is.  It knows parentheses, atoms and offsets, exactly
    like phicad/pylib/sexp.py (which it deliberately mirrors: same helper names, same
    quoted-atom semantics) plus the ability to write back.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

class Atom(str):
    """A bare or quoted token that remembers where it came from."""

    quoted = False
    start = -1
    end = -1

    def __new__(cls, value, quoted=False, start=-1, end=-1):
        obj = super().__new__(cls, value)
        obj.quoted = bool(quoted)
        obj.start = start
        obj.end = end
        return obj


class Q(str):
    """Marker for `fmt()`: render this atom quoted, whatever it looks like."""


class Node(object):
    """A `( … )` list. kids[0] is the head atom when the list is not empty."""

    __slots__ = ("kids", "start", "end", "parent")

    def __init__(self, start, parent=None):
        self.kids = []
        self.start = start
        self.end = -1
        self.parent = parent

    # -- introspection ------------------------------------------------------

    @property
    def head(self):
        """First atom as a plain str, or None for `()` / `((…))`."""
        if self.kids and isinstance(self.kids[0], Atom):
            return str(self.kids[0])
        return None

    def children(self, name=None):
        """Direct child Nodes, optionally filtered by head."""
        out = []
        for k in self.kids:
            if isinstance(k, Node) and (name is None or k.head == name):
                out.append(k)
        return out

    def child(self, name):
        """First direct child Node with that head, or None."""
        for k in self.kids:
            if isinstance(k, Node) and k.head == name:
                return k
        return None

    def atoms(self):
        """Argument atoms (everything after the head)."""
        return [k for k in self.kids[1:] if isinstance(k, Atom)]

    def scalar(self, name, idx=0):
        """atoms(child(name))[idx] as str, or None."""
        c = self.child(name)
        if c is None:
            return None
        a = c.atoms()
        return str(a[idx]) if len(a) > idx else None

    def floats(self, name):
        """Numeric arguments of a named child: (at 1 2 90) -> [1.0, 2.0, 90.0]."""
        c = self.child(name)
        if c is None:
            return None
        out = []
        for a in c.atoms():
            try:
                out.append(float(a))
            except ValueError:
                pass
        return out

    def walk(self):
        """This node and every descendant Node, depth-first."""
        yield self
        for k in self.kids:
            if isinstance(k, Node):
                for n in k.walk():
                    yield n

    def find_all(self, name):
        """Every descendant Node with that head (self included if it matches)."""
        return [n for n in self.walk() if n.head == name]

    def text(self, src):
        """The exact original source of this node."""
        return src[self.start:self.end]

    def __repr__(self):
        return "<Node %s [%d:%d] %d kids>" % (self.head, self.start, self.end, len(self.kids))


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def parse(text):
    """Parse one s-expression document into a Node tree carrying source spans."""
    i, n = 0, len(text)
    stack = []
    root = None
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "(":
            node = Node(i, stack[-1] if stack else None)
            if stack:
                stack[-1].kids.append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            if not stack:
                raise ValueError("unbalanced ')' at offset %d" % i)
            node = stack.pop()
            node.end = i + 1
            if not stack:
                if root is not None:
                    raise ValueError("multiple top-level expressions")
                root = node
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    j += 2
                    continue
                if ch == '"':
                    break
                buf.append(ch)
                j += 1
            if j >= n:
                raise ValueError("unterminated string at offset %d" % i)
            if not stack:
                raise ValueError("stray string at offset %d" % i)
            stack[-1].kids.append(Atom("".join(buf), True, i, j + 1))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            if not stack:
                raise ValueError("stray atom at offset %d" % i)
            stack[-1].kids.append(Atom(text[i:j], False, i, j))
            i = j
    if stack:
        raise ValueError("unbalanced '(' — document truncated?")
    if root is None:
        raise ValueError("empty document")
    return root


# ---------------------------------------------------------------------------
# writer for NEW nodes (KiCad's own layout)
# ---------------------------------------------------------------------------

#: Heads whose runs stay on one line, the way eeschema writes `(pts (xy …) (xy …))`.
INLINE_HEADS = frozenset(("xy",))


def esc(value):
    """Escape a string for a KiCad quoted atom."""
    out = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "")


def num(value):
    """Format a number the way KiCad does: 0, 31.75, 180 — no trailing zeros."""
    f = float(value)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return ("%.6f" % f).rstrip("0").rstrip(".")


def _atom_text(value):
    if isinstance(value, Q):
        return '"%s"' % esc(value)
    if isinstance(value, Atom):
        return ('"%s"' % esc(value)) if value.quoted else str(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return num(value)
    return str(value)


def fmt(spec, level=0, tab="\t"):
    """Render a nested Python list as KiCad-formatted s-expression text.

    spec  -> [head, atom_or_spec, …]; str/int/float/bool are atoms, Q() is a quoted atom,
    list/tuple is a nested node.  A node with no nested children stays on one line.
    `level` is the indent depth the OUTERMOST node is written at (its own leading indent is
    NOT emitted — the caller places it).
    """
    if not isinstance(spec, (list, tuple)):
        return _atom_text(spec)
    head = spec[0]
    parts = [_atom_text(head)]
    kids = list(spec[1:])
    nested = [k for k in kids if isinstance(k, (list, tuple))]
    plain = [k for k in kids if not isinstance(k, (list, tuple))]
    if not nested:
        for p in plain:
            parts.append(_atom_text(p))
        return "(" + " ".join(parts) + ")"

    for p in plain:
        parts.append(_atom_text(p))
    lines = ["(" + " ".join(parts)]
    pad = tab * (level + 1)
    run = []                                  # consecutive INLINE_HEADS children
    for k in kids:
        if not isinstance(k, (list, tuple)):
            continue
        if isinstance(k[0], str) and str(k[0]) in INLINE_HEADS:
            run.append(fmt(k, level + 1, tab))
            continue
        if run:
            lines.append(pad + " ".join(run))
            run = []
        lines.append(pad + fmt(k, level + 1, tab))
    if run:
        lines.append(pad + " ".join(run))
    lines.append(tab * level + ")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the document: queued span edits
# ---------------------------------------------------------------------------

class EditConflict(ValueError):
    """Two queued edits overlap — the caller asked for something ambiguous."""


class StaleWrite(ValueError):
    """The file changed on disk between load and save — refuse to clobber the other writer.

    Two tools editing one document is read-modify-write: without this check the second
    write silently discarded the first edit and still reported success.
    """


def _stamp(path):
    """Cheap identity of a file's contents: (size, mtime_ns). None when absent."""
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class Doc(object):
    """A parsed document plus a queue of span edits. Untouched text is preserved exactly."""

    def __init__(self, text, path=None, stamp=None):
        self.text = text
        self.path = path
        self.root = parse(text)
        self._edits = []          # (start, end, replacement, label)
        # (size, mtime_ns) as of load, so save() can detect a competing writer.
        self.load_stamp = stamp

    @classmethod
    def load(cls, path):
        # Stamp BEFORE reading: if a writer lands between stat and read we compare against
        # the older stamp and fail closed rather than silently accepting a torn read.
        stamp = _stamp(path)
        with open(str(path), "r", encoding="utf-8") as fh:
            return cls(fh.read(), str(path), stamp)

    # -- queueing -----------------------------------------------------------

    @property
    def dirty(self):
        return bool(self._edits)

    @property
    def edit_count(self):
        return len(self._edits)

    def edits(self):
        """Queued edits as dicts, for reporting what a tool actually did."""
        return [{"start": s, "end": e, "label": lab, "bytes_in": e - s,
                 "bytes_out": len(r)} for (s, e, r, lab) in self._edits]

    def replace(self, start, end, replacement, label=""):
        """Replace the character span [start, end) with `replacement`."""
        if start < 0 or end > len(self.text) or start > end:
            raise ValueError("bad span [%d:%d)" % (start, end))
        self._edits.append((start, end, replacement, label))

    def insert(self, pos, text, label=""):
        """Insert text at a character offset."""
        self.replace(pos, pos, text, label)

    def replace_atom(self, atom, value, quoted=None, label=""):
        """Rewrite one atom in place, keeping its quoting unless told otherwise."""
        if not isinstance(atom, Atom) or atom.start < 0:
            raise ValueError("not a source-backed atom")
        q = atom.quoted if quoted is None else quoted
        new = ('"%s"' % esc(value)) if q else _atom_text(value)
        self.replace(atom.start, atom.end, new, label or "atom")

    def replace_node(self, node, text, label=""):
        """Replace a whole `( … )` node with new text."""
        self.replace(node.start, node.end, text, label or ("node:%s" % node.head))

    def line_indent(self, pos):
        """The whitespace run that starts the line `pos` sits on."""
        bol = self.text.rfind("\n", 0, pos) + 1
        i = bol
        while i < len(self.text) and self.text[i] in " \t":
            i += 1
        return self.text[bol:i]

    def depth_of(self, node):
        """How many ancestors a node has (root = 0) — its natural indent level."""
        depth, cur = 0, node.parent
        while cur is not None:
            depth += 1
            cur = cur.parent
        return depth

    def remove_node(self, node, label=""):
        """Delete a node along with the indent that introduced it and its line break.

        Removing `(symbol …)` must not leave a blank indented line behind, so the span
        grows left over the run of spaces/tabs and, when that reaches the line start, over
        the preceding newline too.
        """
        start = node.start
        i = start - 1
        while i >= 0 and self.text[i] in " \t":
            i -= 1
        if i >= 0 and self.text[i] == "\n":
            start = i
        else:
            start = i + 1
        end = node.end
        if end < len(self.text) and self.text[end] == "\n" and start == node.start:
            end += 1
        self.replace(start, end, "", label or ("remove:%s" % node.head))

    def append_child(self, parent, spec_or_text, label=""):
        """Append a new child node inside `parent`, indented like its siblings.

        `spec_or_text` is either a nested list for fmt() or ready-made text.  The indent is
        taken from the parent's last child when there is one (so a hand-formatted file keeps
        its own convention) and derived from the tree depth otherwise.
        """
        kids = parent.children()
        if kids:
            indent = self.line_indent(kids[-1].start) or ("\t" * (self.depth_of(parent) + 1))
            anchor = kids[-1].end
        else:
            indent = "\t" * (self.depth_of(parent) + 1)
            anchor = parent.end - 1
        level = len(indent.replace("    ", "\t"))
        if isinstance(spec_or_text, (list, tuple)):
            body = fmt(spec_or_text, level)
        else:
            body = str(spec_or_text)
        text = "\n" + indent + body if kids else "\n" + indent + body + "\n" + indent[:-1]
        self.insert(anchor, text, label or "append")
        return text

    # -- rendering ----------------------------------------------------------

    def render(self):
        """Apply every queued edit (rightmost first) and return the new document text."""
        if not self._edits:
            return self.text
        ordered = sorted(self._edits, key=lambda e: (e[0], e[1]))
        prev_end = -1
        for (s, e, _r, lab) in ordered:
            if s < prev_end:
                raise EditConflict("overlapping edit at %d (%s)" % (s, lab))
            prev_end = e
        out = self.text
        for (s, e, r, _lab) in reversed(ordered):
            out = out[:s] + r + out[e:]
        return out

    def save(self, path, check_stale=True):
        """Write the edited document atomically. Returns the number of bytes written.

        When `path` is the file this document was loaded from, the on-disk stamp must still
        match the one taken at load time; otherwise another writer got there first and
        `StaleWrite` is raised instead of destroying their edit.  Pass `check_stale=False`
        for a deliberate overwrite of an unrelated target.

        The write goes to a temp file in the same directory and is then `os.replace`d, so a
        reader never observes a half-written document and a crash cannot truncate the input.
        """
        data = self.render()
        target = Path(str(path))
        same_file = (self.path is not None
                     and Path(str(self.path)).resolve() == target.resolve())
        if check_stale and same_file and self.load_stamp is not None:
            now = _stamp(target)
            if now is not None and now != self.load_stamp:
                raise StaleWrite(
                    "%s changed on disk since it was read (size/mtime %s -> %s); "
                    "re-read and re-apply the edit" % (target, self.load_stamp, now))
        encoded = data.encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(target))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(encoded)
