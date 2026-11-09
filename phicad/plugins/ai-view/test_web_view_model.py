#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Interactive GLB export and the token-gated model route."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import web_view                                             # noqa: E402


class ModelRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # resolve(): serve() resolves the board path, and macOS's /var -> /private/var symlink
        # would otherwise make the argv comparison below fail on every developer laptop.
        root = Path(self.tmp.name).resolve()
        self.board = root / "model.kicad_pcb"
        self.board.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
        self.calls = root / "calls.jsonl"
        self.cli = root / "fake-kicad-cli"
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"log = pathlib.Path({str(self.calls)!r})\n"
            "with log.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "out = pathlib.Path(args[args.index('-o') + 1])\n"
            "out.write_bytes(b'glTF' + b'\\0' * 20)\n",
            encoding="utf-8",
        )
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.previous_cli = os.environ.get("PHICAD_KICAD_CLI")
        os.environ["PHICAD_KICAD_CLI"] = str(self.cli)
        self.server, self.url = web_view.serve(self.board, port=0, token="model-token")
        self.base = self.url.split("/?", 1)[0]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.previous_cli is None:
            os.environ.pop("PHICAD_KICAD_CLI", None)
        else:
            os.environ["PHICAD_KICAD_CLI"] = self.previous_cli
        self.tmp.cleanup()

    def request(self, path: str, method: str = "GET", token: bool = True):
        headers = {"X-PhiCad-Token": "model-token"} if token else {}
        data = b"{}" if method == "POST" else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=30)
            return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_model_is_404_until_exported_then_served_as_glb(self):
        status, _, _ = self.request("/api/model")
        self.assertEqual(status, 404)

        status, _, body = self.request("/api/model", method="POST")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, headers, body = self.request("/api/model")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "model/gltf-binary")
        self.assertTrue(body.startswith(b"glTF"))

        args = json.loads(self.calls.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(args[:3], ["pcb", "export", "glb"])
        self.assertIn("--force", args)
        self.assertEqual(args[-1], str(self.board))

    def test_model_route_requires_token(self):
        status, _, _ = self.request("/api/model", token=False)
        self.assertEqual(status, 403)

    def test_export_failure_returns_a_visible_reason(self):
        os.environ["PHICAD_KICAD_CLI"] = str(self.cli.with_name("missing-cli"))
        status, _, body = self.request("/api/model", method="POST")
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertIn("Interactive 3D export failed", error)
        self.assertIn("not executable", error)

    def test_content_hash_reuses_then_invalidates_export(self):
        self.request("/api/model", method="POST")
        self.request("/api/model", method="POST")
        self.assertEqual(len(self.calls.read_text(encoding="utf-8").splitlines()), 1)

        self.board.write_text("(kicad_pcb (version 20240108) (generator changed))\n",
                              encoding="utf-8")
        self.request("/api/model", method="POST")
        self.assertEqual(len(self.calls.read_text(encoding="utf-8").splitlines()), 2)

    def test_page_loads_viewer_and_optional_decoders_only_from_loopback(self):
        status, _, body = self.request("/?token=model-token")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn('/static/model-viewer.min.js?token=model-token', page)
        self.assertIn('dracoDecoderLocation: "/static/model-viewer-decoders/"', page)
        self.assertNotIn("gstatic.com", page)
        self.assertNotIn("cdn.jsdelivr.net", page)

    def test_vendored_viewer_route_is_token_gated(self):
        status, _, _ = self.request("/static/model-viewer.min.js", token=False)
        self.assertEqual(status, 403)
        status, headers, body = self.request("/static/model-viewer.min.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/javascript")
        self.assertGreater(len(body), 1_000_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
