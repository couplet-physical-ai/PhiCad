#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Backend selection and PDFium adapter tests for the BOM/Datasheets tab."""

from __future__ import annotations

import builtins
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import bom_view
except ModuleNotFoundError as exc:
    if exc.name not in ("tkinter", "_tkinter"):
        raise
    # The backend helpers do not construct widgets.  Keep their tests runnable on the
    # headless Python used by the fast gate even though the production module imports Tk
    # for the actual BOM tab.
    fake_tk = types.ModuleType("tkinter")
    fake_tk.CENTER = "center"
    fake_tk.W = "w"
    fake_ttk = types.ModuleType("tkinter.ttk")
    fake_tk.ttk = fake_ttk
    missing = object()
    old_tk = sys.modules.get("tkinter", missing)
    old_ttk = sys.modules.get("tkinter.ttk", missing)
    sys.modules["tkinter"] = fake_tk
    sys.modules["tkinter.ttk"] = fake_ttk
    try:
        import bom_view
    finally:
        if old_tk is missing:
            sys.modules.pop("tkinter", None)
        else:
            sys.modules["tkinter"] = old_tk
        if old_ttk is missing:
            sys.modules.pop("tkinter.ttk", None)
        else:
            sys.modules["tkinter.ttk"] = old_ttk


def _pdfium_importable() -> bool:
    try:
        import pypdfium2  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


class PreviewBackendTests(unittest.TestCase):
    def test_linux_prefers_pdfium_when_both_backends_are_available(self) -> None:
        with patch.object(bom_view.sys, "platform", "linux"), \
                patch.object(bom_view.shutil, "which", return_value="/usr/bin/qlmanage"), \
                patch.object(bom_view, "_load_pdfium", return_value=object()):
            self.assertEqual(bom_view.preview_backend(), "pypdfium2")

    def test_linux_falls_back_to_qlmanage_when_pdfium_is_missing(self) -> None:
        with patch.object(bom_view.sys, "platform", "linux"), \
                patch.object(bom_view.shutil, "which", return_value="/usr/bin/qlmanage"), \
                patch.object(bom_view, "_load_pdfium", return_value=None):
            self.assertEqual(bom_view.preview_backend(), "qlmanage")

    def test_macos_prefers_qlmanage_without_importing_pdfium(self) -> None:
        with patch.object(bom_view.sys, "platform", "darwin"), \
                patch.object(bom_view.shutil, "which", return_value="/usr/bin/qlmanage"), \
                patch.object(bom_view, "_load_pdfium") as load_pdfium:
            self.assertEqual(bom_view.preview_backend(), "qlmanage")
            load_pdfium.assert_not_called()

    def test_missing_backends_are_reported(self) -> None:
        original_import = builtins.__import__

        def missing_pdfium(name, *args, **kwargs):
            if name == "pypdfium2":
                raise ImportError("test package absent")
            return original_import(name, *args, **kwargs)

        with patch.object(bom_view.sys, "platform", "linux"), \
                patch.object(bom_view.shutil, "which", return_value=None), \
                patch.object(builtins, "__import__", missing_pdfium):
            self.assertIsNone(bom_view.preview_backend())

    def test_missing_backends_name_the_install_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "sheet.pdf"
            pdf.write_bytes(b"%PDF-fake")
            with patch.object(bom_view.sys, "platform", "linux"), \
                    patch.object(bom_view.shutil, "which", return_value=None), \
                    patch.object(bom_view, "_load_pdfium", return_value=None):
                with self.assertRaisesRegex(bom_view.PreviewError,
                                             "build/venv/bin/pip install pypdfium2"):
                    bom_view.preview_png(str(pdf), directory)


@unittest.skipUnless(_pdfium_importable(), "pypdfium2 is not importable")
class PdfiumAdapterTests(unittest.TestCase):
    """Use a fake PDFium document: the suite never needs a real PDF renderer."""

    class _Image:
        def save(self, path, format=None):
            Path(path).write_bytes(b"fake png")

    class _Bitmap:
        def to_pil(self):
            return PdfiumAdapterTests._Image()

        def close(self):
            pass

    class _TextPage:
        def get_text_range(self):
            return "  regulator\n3.3 V  "

        def close(self):
            pass

    class _Page:
        def get_width(self):
            return 612

        def get_height(self):
            return 792

        def render(self, scale):
            self.scale = scale
            return PdfiumAdapterTests._Bitmap()

        def get_textpage(self):
            return PdfiumAdapterTests._TextPage()

        def close(self):
            pass

    class _Document:
        def __init__(self, path):
            self.page = PdfiumAdapterTests._Page()

        def __len__(self):
            return 1

        def __getitem__(self, index):
            return self.page

        def close(self):
            pass

    class _Pdfium:
        pass

    def test_preview_scales_longest_edge_and_writes_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "sheet.pdf"
            pdf.write_bytes(b"%PDF-fake")
            out_dir = Path(directory) / "renders"
            self._Pdfium.PdfDocument = self._Document
            # Exercise the PDFium adapter even on macOS, where Quick Look is preferred.
            with patch.object(bom_view, "preview_backend", return_value="pypdfium2"), \
                    patch.object(bom_view, "_load_pdfium", return_value=self._Pdfium):
                output = bom_view.preview_png(str(pdf), str(out_dir), size=1400)
            self.assertEqual(Path(output).read_bytes(), b"fake png")
            self.assertEqual(Path(output).name, "sheet.pdf.png")

    def test_datasheet_text_joins_pages_without_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "sheet.pdf"
            pdf.write_bytes(b"%PDF-fake")
            self._Pdfium.PdfDocument = self._Document
            with patch.object(bom_view, "_load_pdfium", return_value=self._Pdfium):
                text = bom_view.datasheet_text(str(pdf), max_pages=3)
            self.assertEqual(text, "regulator\n3.3 V")


if __name__ == "__main__":
    unittest.main()
