#!/usr/bin/env python3
"""Regression tests for the two format assumptions that broke the parts -> synth path.

Both bugs had the same shape: synth accepted only the exact bytes KiCad's own serializer
emits, and silently rejected valid files written by anything else -- specifically
`easyeda2kicad`, which is the fetcher behind `phicad/parts`. So the exact-parts pipeline
could fetch a real part that synth then reported as "not in any library" or as a library
with zero symbols. Neither had a test, which is why both survived.

Pure text/parser assertions: no venv, no built kicad-cli, no network.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# `library` puts phicad/pylib on sys.path as an import side effect, so it has to come
# before `sexp` -- which is why these are not alphabetical.
from library import FOOTPRINT_HEADS, LibraryError, footprint_from_node  # noqa: E402
from symbols import _is_top_level  # noqa: E402

import sexp  # noqa: E402  (path set by the `library` import above)


def _tops(text: str) -> list:
    """Every `(symbol "NAME"` in `text`, paired with its top-level verdict."""
    out = []
    i = 0
    needle = '(symbol "'
    while True:
        i = text.find(needle, i)
        if i < 0:
            return out
        start = i + len(needle)
        out.append((text[start:text.find('"', start)], _is_top_level(text, i)))
        i += 1


class SymbolDepthTests(unittest.TestCase):
    """`_is_top_level` must measure paren depth, not indentation style."""

    def test_space_indented_library_indexes_its_symbols(self):
        """easyeda2kicad indents with two spaces; that library must not read as empty."""
        lib = ('(kicad_symbol_lib\n'
               '  (symbol "C_0805"\n'
               '    (symbol "C_0805_1_1"\n'
               '      (pin passive line)\n'
               '    )\n'
               '  )\n'
               '  (symbol "R_0805"\n'
               '  )\n'
               ')\n')
        self.assertEqual(
            _tops(lib),
            [("C_0805", True), ("C_0805_1_1", False), ("R_0805", True)],
        )

    def test_tab_indented_library_still_works(self):
        """KiCad's own serializer emits tabs -- the old behaviour must be preserved."""
        lib = ('(kicad_symbol_lib\n'
               '\t(symbol "C_0805"\n'
               '\t\t(symbol "C_0805_1_1"\n'
               '\t\t)\n'
               '\t)\n'
               ')\n')
        self.assertEqual(_tops(lib), [("C_0805", True), ("C_0805_1_1", False)])

    def test_parens_inside_a_property_value_are_text_not_structure(self):
        """A value like "100nF (104) +/-10% 50V" must not shift the measured depth."""
        lib = ('(kicad_symbol_lib\n'
               '  (symbol "C1"\n'
               '    (property "Value" "100nF (104) +/-10% 50V")\n'
               '  )\n'
               '  (symbol "C2"\n'
               '  )\n'
               ')\n')
        self.assertEqual(_tops(lib), [("C1", True), ("C2", True)])

    def test_an_escaped_quote_does_not_end_the_string_early(self):
        """A 3/4" label keeps its parens inert: the next symbol is still top level."""
        lib = ('(kicad_symbol_lib\n'
               '  (symbol "S1"\n'
               '    (property "Desc" "3/4\\" hex ((weird))")\n'
               '  )\n'
               '  (symbol "S2"\n'
               '  )\n'
               ')\n')
        self.assertEqual(_tops(lib), [("S1", True), ("S2", True)])

    def test_a_nested_unit_is_never_reported_as_top_level(self):
        """The gate has to be able to fail: depth 2 must stay False."""
        lib = '(kicad_symbol_lib\n  (symbol "U1"\n    (symbol "U1_1_1"\n    )\n  )\n)\n'
        self.assertEqual(dict(_tops(lib))["U1_1_1"], False)


class FootprintHeadTests(unittest.TestCase):
    """`module` is the pre-6.0 token and easyeda2kicad still writes it today."""

    def test_both_tokens_are_accepted(self):
        self.assertEqual(FOOTPRINT_HEADS, ("footprint", "module"))

    def test_a_module_node_parses_like_a_footprint_node(self):
        """Identical bodies under either head must yield the same pads."""
        body = ('  (layer "F.Cu")\n'
                '  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))\n'
                '  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))\n')
        parsed = {}
        for head in FOOTPRINT_HEADS:
            doc = sexp.parse(f'({head} "R_0805"\n{body})\n')
            fp = footprint_from_node(doc, lib="test", name="R_0805")
            parsed[head] = sorted(fp.pads)
        self.assertEqual(parsed["module"], ["1", "2"])
        self.assertEqual(parsed["module"], parsed["footprint"])

    def test_a_genuinely_wrong_head_is_still_refused(self):
        """A gate that cannot fail is not a gate: only these two heads are legal."""
        doc = sexp.parse('(kicad_symbol_lib "nope")\n')
        with self.assertRaises(LibraryError) as caught:
            footprint_from_node(doc, lib="test", name="nope")
        message = str(caught.exception)
        self.assertIn("not a footprint", message)
        # The error names what was allowed, so the reader can act on it.
        self.assertIn("footprint, module", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
